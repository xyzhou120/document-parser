"""执行发件箱记录的至少一次投递。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import ParseRun
from app.models.execution import ExecutionOutbox, ParseFieldJob

from .field_scheduler import ACTIVE_RUN_STATES


class DispatchOutbox:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def claim_field_runs(self, *, now: datetime | None = None, limit: int = 100,
                               run_id=None, run_ids=None) -> list[ExecutionOutbox]:
        """每次运行最多领取一个唤醒事件，并按上次投递轮次排序。

        所选运行中到期的字段事件在一次 SQL 更新中合并。
        消息代理故障时代表事件仍可重试。运行锁在不调用提供方的情况下
        串行化并发调度器。
        """
        now = now or datetime.utcnow()
        due = (ExecutionOutbox.dispatch_state.in_(["pending", "failed"]),
               or_(ExecutionOutbox.next_attempt_at.is_(None), ExecutionOutbox.next_attempt_at <= now))
        field_jobs = select(ParseFieldJob.job_id).where(
            ParseFieldJob.run_id == ParseRun.parse_run_id).correlate(ParseRun)
        pending = exists(select(ExecutionOutbox.event_id).where(
            ExecutionOutbox.kind == "field_job_wakeup", ExecutionOutbox.aggregate_id.in_(field_jobs), *due))
        sending = exists(select(ExecutionOutbox.event_id).where(
            ExecutionOutbox.kind == "field_job_wakeup", ExecutionOutbox.aggregate_id.in_(field_jobs),
            or_(ExecutionOutbox.dispatch_state == "sending",
                (ExecutionOutbox.dispatch_state == "sent") &
                (ExecutionOutbox.payload["dispatch_outstanding"].as_boolean().is_(True)))))
        last_sent = select(func.max(ExecutionOutbox.updated_at)).where(
            ExecutionOutbox.kind == "field_job_wakeup", ExecutionOutbox.aggregate_id.in_(field_jobs),
            ExecutionOutbox.dispatch_state == "sent").correlate(ParseRun).scalar_subquery()
        live_worker = exists(select(ParseFieldJob.job_id).where(
            ParseFieldJob.run_id == ParseRun.parse_run_id,
            ParseFieldJob.execution_state == "running", ParseFieldJob.lease_until > now))
        # yield 事件在现有执行窗口中保持 pending，窗口交还租约后再投递。
        query = select(ParseRun).where(ParseRun.status.in_(ACTIVE_RUN_STATES), pending, ~sending, ~live_worker)
        if run_id is not None:
            query = query.where(ParseRun.parse_run_id == run_id)
        if run_ids is not None:
            query = query.where(ParseRun.parse_run_id.in_(run_ids))
        runs = list((await self.db.execute(query.order_by(last_sent.asc().nulls_first(),
            ParseRun.created_at, ParseRun.parse_run_id).limit(limit)
            .with_for_update(skip_locked=True).execution_options(populate_existing=True))).scalars().all())
        representatives = []
        for run in runs:
            jobs = select(ParseFieldJob.job_id).where(ParseFieldJob.run_id == run.parse_run_id)
            wake = await self.db.scalar(select(ExecutionOutbox).where(
                ExecutionOutbox.kind == "field_job_wakeup", ExecutionOutbox.aggregate_id.in_(jobs), *due
            ).order_by(ExecutionOutbox.created_at, ExecutionOutbox.event_id).limit(1).with_for_update())
            if wake is None:
                continue
            await self.db.execute(update(ExecutionOutbox).where(
                ExecutionOutbox.kind == "field_job_wakeup", ExecutionOutbox.aggregate_id.in_(jobs),
                ExecutionOutbox.event_id != wake.event_id, *due
            ).values(dispatch_state="coalesced", last_error=None, next_attempt_at=None, updated_at=now))
            wake.dispatch_state = "sending"
            wake.attempts += 1
            wake.updated_at = now
            wake.payload = {**dict(wake.payload or {}), "run_id": str(run.parse_run_id),
                            "execution_generation": run.execution_generation,
                            "dispatch_outstanding": True, "dispatch_event_id": str(wake.event_id),
                            "dispatch_attempt": wake.attempts}
            representatives.append(wake)
        # 终止或受控运行可以保留审计事件，但不能进入 Celery。
        # 这也会吸收取消后遗留的旧队列事件。
        blocked_jobs = select(ParseFieldJob.job_id).join(ParseRun, ParseRun.parse_run_id == ParseFieldJob.run_id).where(
            ParseRun.status.not_in(ACTIVE_RUN_STATES))
        await self.db.execute(update(ExecutionOutbox).where(
            ExecutionOutbox.kind == "field_job_wakeup", ExecutionOutbox.aggregate_id.in_(blocked_jobs), *due
        ).values(dispatch_state="suppressed", last_error=None, next_attempt_at=None, updated_at=now))
        return representatives

    async def claim_due(self, *, now: datetime | None = None, limit: int = 100,
                        kinds: list[str] | None = None, aggregate_ids: list | None = None) -> list[ExecutionOutbox]:
        now = now or datetime.utcnow()
        query = select(ExecutionOutbox).where(
                ExecutionOutbox.dispatch_state.in_(["pending", "failed"]),
                or_(ExecutionOutbox.next_attempt_at.is_(None), ExecutionOutbox.next_attempt_at <= now),
            )
        if kinds is not None:
            query = query.where(ExecutionOutbox.kind.in_(kinds))
        if aggregate_ids is not None:
            query = query.where(ExecutionOutbox.aggregate_id.in_(aggregate_ids))
        rows = (await self.db.execute(query.order_by(ExecutionOutbox.created_at.asc())
                                     .limit(limit).with_for_update(skip_locked=True))).scalars().all()
        for event in rows:
            event.dispatch_state = "sending"
            event.attempts += 1
        return list(rows)

    async def dispatch(self, event: ExecutionOutbox, sender: Callable[[str, dict[str, Any]], Awaitable[None]]) -> bool:
        try:
            await sender(event.kind, dict(event.payload or {}))
        except Exception as exc:
            await self.mark_failed(event, str(exc))
            return False
        await self.mark_sent(event)
        return True

    async def mark_sent(self, event: ExecutionOutbox) -> None:
        event.dispatch_state = "sent"
        event.next_attempt_at = (datetime.utcnow() + timedelta(minutes=5)
                                 if (event.payload or {}).get("dispatch_outstanding") else None)
        event.last_error = None

    async def mark_failed(self, event: ExecutionOutbox, error: str, *, now: datetime | None = None,
                          max_backoff_seconds: int = 300) -> None:
        now = now or datetime.utcnow()
        event.dispatch_state = "failed"
        event.last_error = error[:4000]
        delay = min(max_backoff_seconds, 2 ** min(event.attempts, 8))
        event.next_attempt_at = now + timedelta(seconds=delay)

    async def recover_stuck(self, *, before: datetime, limit: int = 100,
                            kinds: list[str] | None = None) -> int:
        query = select(ExecutionOutbox).where(or_(
            (ExecutionOutbox.dispatch_state == "sending") & (ExecutionOutbox.updated_at < before),
            (ExecutionOutbox.dispatch_state == "sent") &
            ExecutionOutbox.payload["dispatch_outstanding"].as_boolean().is_(True) &
            (ExecutionOutbox.next_attempt_at <= datetime.utcnow()),
        ))
        if kinds is not None:
            query = query.where(ExecutionOutbox.kind.in_(kinds))
        rows = (await self.db.execute(query.limit(limit).with_for_update(skip_locked=True))).scalars().all()
        for event in rows:
            event.dispatch_state = "failed"
            event.next_attempt_at = datetime.utcnow()
            event.last_error = "dispatcher lease expired; retrying"
        return len(rows)
