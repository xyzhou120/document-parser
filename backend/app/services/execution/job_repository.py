"""字段执行任务的异步 SQLAlchemy 仓库。

仓库从不提交事务。因此调用方可在同一事务中原子持久化检查点和发件箱事件。
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import ExtractedFieldModel, ParseRun
from app.models.execution import ExecutionOutbox, ParseFieldJob

from .field_scheduler import (
    ACTIVE_FIELD_STATES,
    ACTIVE_RUN_STATES,
    LeaseToken,
    QuantumOutcome,
    Scheduler,
    StaleLeaseError,
    aggregate_run_status,
    require_lease,
    wakeup_dedupe_key,
)

# 执行已经收口的状态。取消和被新版本替代的运行不在这里改计数。
_COUNTED_RUN_STATUSES = {"COMPLETED", "COMPLETED_WITH_REVIEW", "FAILED"}
# 这些状态没有可展示的抽出值。review_required 不在其中，仍需核对的值照常计数。
_WITHOUT_EXTRACTED_VALUE = {"missing", "blocked", "incomplete", "not_found"}
RELIABLE_CONTRACT = "reliable-extraction-v2"


def field_has_extracted_value(field: Any) -> bool:
    """字段已经抽出值。仍需人工核对的结果计入，范围内未找到和未完成不计入。"""
    if getattr(field, "validation_status", None) in _WITHOUT_EXTRACTED_VALUE:
        return False
    if getattr(field, "field_value", None) is not None:
        return True
    return bool(getattr(field, "instances", None))


def count_extracted_fields(fields: Iterable[Any]) -> int:
    """按字段键计数，同一字段的多条事实只算一个字段。"""
    return len({field.field_key for field in fields if field_has_extracted_value(field)})


async def write_extracted_field_counts(
    db: AsyncSession, runs: Iterable[ParseRun], *, repair_zeros: bool = False,
) -> None:
    """把已抽出值的字段数写回运行记录。

    repair_zeros 只修补可靠执行里仍为 0 的已结束运行，避免改写旧引擎已经写好的计数。
    """
    chosen: list[ParseRun] = []
    for run in runs:
        if run.status not in _COUNTED_RUN_STATUSES:
            continue
        stale_reliable_zero = (
            getattr(run, "contract_version", None) == RELIABLE_CONTRACT and (run.fields_extracted or 0) == 0
        )
        if repair_zeros and not stale_reliable_zero:
            continue
        chosen.append(run)
    if not chosen:
        return
    rows = (await db.execute(
        select(ExtractedFieldModel).where(
            ExtractedFieldModel.parse_run_id.in_([run.parse_run_id for run in chosen])
        )
    )).scalars().all()
    grouped: dict[Any, list[Any]] = {}
    for row in rows:
        grouped.setdefault(row.parse_run_id, []).append(row)
    for run in chosen:
        count = count_extracted_fields(grouped.get(run.parse_run_id, []))
        if run.fields_extracted != count:
            run.fields_extracted = count


class JobRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def initialize_jobs(self, run_id: UUID, *, revision_id: UUID, fields: list[dict[str, Any]]) -> list[ParseFieldJob]:
        """为一次运行幂等创建不可变的字段任务分母。

        fields 必须已经是冻结的规则快照。本方法不读取当前规则配置，
        也不删除现有字段任务，因此跨数据库与消息边界重试时仍然安全。
        """
        run = await self.db.get(ParseRun, run_id, with_for_update=True)
        if run is None:
            raise KeyError(f"run {run_id} not found")
        existing = {
            row.field_key: row
            for row in (await self.db.execute(select(ParseFieldJob).where(ParseFieldJob.run_id == run_id))).scalars().all()
        }
        if existing:
            wanted = {str(spec["field_key"]): str(spec["rule_hash"]) for spec in fields}
            if set(wanted) != set(existing) or any(existing[key].rule_hash != value or existing[key].revision_id != revision_id for key, value in wanted.items()):
                raise ValueError("existing run job snapshot cannot be changed; create a child run")
            return list(existing.values())
        profile = dict(run.extraction_profile or {})
        if profile.get("extraction_strategy") == "page_batch_v1":
            # One frozen field owns page traversal. Its guarded publication
            # writes the shared page result to every field in the same run.
            profile["coordinator_field_key"] = min(str(spec["field_key"]) for spec in fields)
            run.extraction_profile = profile
        result = list(existing.values())
        for spec in fields:
            key = str(spec["field_key"])
            if key in existing:
                continue
            job = ParseFieldJob(run_id=run_id, field_key=key, rule_hash=str(spec["rule_hash"]),
                                revision_id=revision_id, execution_state="queued", semantic_state="unknown",
                                phase="planned", checkpoint={"rule_snapshot": spec.get("snapshot", {})},
                                fence_token=0, progress_seq=0, attempt=0)
            self.db.add(job)
            result.append(job)
        await self.db.flush()
        return result

    async def _locked(self, job_id: UUID) -> tuple[ParseFieldJob, ParseRun]:
        run_id = await self.db.scalar(select(ParseFieldJob.run_id).where(ParseFieldJob.job_id == job_id))
        if run_id is None:
            raise KeyError(f"field job {job_id} not found")
        # 领取、发布、控制和恢复统一使用运行 -> 任务的加锁顺序。
        # populate_existing 防止长生命周期会话在等待锁后接受缓存的版本或栅栏值。
        run = await self.db.scalar(select(ParseRun).where(ParseRun.parse_run_id == run_id)
                                   .with_for_update().execution_options(populate_existing=True))
        job = await self.db.scalar(select(ParseFieldJob).where(ParseFieldJob.job_id == job_id)
                                   .with_for_update().execution_options(populate_existing=True))
        if run is None or job is None:
            raise KeyError(job_id)
        return job, run

    async def claim_next(self, *, run_id: UUID | None, owner: str, now: datetime | None = None,
                         lease_seconds: int = 90) -> LeaseToken | None:
        now = now or datetime.utcnow()
        ready = (
            ParseFieldJob.execution_state.in_(["queued", "yielded"]),
            or_(ParseFieldJob.next_attempt_at.is_(None), ParseFieldJob.next_attempt_at <= now),
        )
        eligible = or_(ParseRun.extraction_profile["extraction_strategy"].as_string().is_(None),
            ParseRun.extraction_profile["extraction_strategy"].as_string() != "page_batch_v1",
            ParseFieldJob.field_key == ParseRun.extraction_profile["coordinator_field_key"].as_string())
        runnable = select(ParseFieldJob.job_id).where(ParseFieldJob.run_id == ParseRun.parse_run_id,
                                                    *ready, eligible).exists()
        runs = select(ParseRun).where(ParseRun.status.in_(ACTIVE_RUN_STATES), runnable)
        if run_id is not None:
            runs = runs.where(ParseRun.parse_run_id == run_id)
        run = await self.db.scalar(runs.order_by(ParseRun.created_at.asc(), ParseRun.parse_run_id)
                                  .limit(1).with_for_update(skip_locked=True).execution_options(populate_existing=True))
        if run is None:
            return None
        query = (
            select(ParseFieldJob)
            .where(
                ParseFieldJob.run_id == run.parse_run_id, *ready,
            )
            .order_by(ParseFieldJob.attempt.asc(), ParseFieldJob.created_at.asc(), ParseFieldJob.field_key.asc())
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        profile = run.extraction_profile or {}
        if profile.get("extraction_strategy") == "page_batch_v1":
            query = query.where(ParseFieldJob.field_key == profile["coordinator_field_key"])
        job = await self.db.scalar(query.execution_options(populate_existing=True))
        if job is None:
            return None
        run.status = "RUNNING"
        if run.started_at is None:
            run.started_at = now
        return Scheduler.claim(job, run, owner, now=now, lease_seconds=lease_seconds)

    async def requeue_due(self, *, now: datetime | None = None, limit: int = 100) -> int:
        now = now or datetime.utcnow()
        rows = (await self.db.execute(
            select(ParseFieldJob.job_id).join(ParseRun, ParseRun.parse_run_id == ParseFieldJob.run_id).where(
                ParseRun.status.in_(ACTIVE_RUN_STATES),
                or_(and_(ParseFieldJob.execution_state == "yielded",
                         or_(ParseFieldJob.next_attempt_at.is_(None), ParseFieldJob.next_attempt_at <= now)),
                    and_(ParseFieldJob.execution_state.in_(["waiting_service", "waiting_external_quota"]),
                         ParseFieldJob.next_attempt_at.is_not(None), ParseFieldJob.next_attempt_at <= now),
                    and_(ParseFieldJob.execution_state == "running", ParseFieldJob.lease_until <= now)),
            ).order_by(ParseFieldJob.created_at.asc()).limit(limit)
        )).scalars().all()
        count = 0
        for job_id in rows:
            job, run = await self._locked(job_id)
            if run.status not in ACTIVE_RUN_STATES:
                continue
            due = (job.execution_state == "yielded" and (job.next_attempt_at is None or job.next_attempt_at <= now))
            due |= (job.execution_state in {"waiting_service", "waiting_external_quota"}
                    and job.next_attempt_at is not None and job.next_attempt_at <= now)
            expired = job.execution_state == "running" and job.lease_until is not None and job.lease_until <= now
            if not due and not expired:
                continue
            job.execution_state = "queued"
            job.fence_token += 1
            job.next_attempt_at = None
            job.lease_owner = None
            job.lease_until = None
            job.updated_at = now
            await self.enqueue_outbox(kind="field_job_wakeup", aggregate_id=job.job_id,
                                      dedupe_key=wakeup_dedupe_key(job.job_id, job.progress_seq),
                                      payload={"job_id": str(job.job_id), "run_id": str(run.parse_run_id)})
            count += 1
        return count

    async def publish(self, token: LeaseToken, outcome: QuantumOutcome, *, writer: Any = None) -> None:
        """原子保护并持久化领域结果、检查点和投递事件。"""
        job, run = await self._locked(token.job_id)
        require_lease(job, run, token)
        if writer is not None:
            await writer(self.db, job, outcome)
        state = getattr(outcome, "state", None)
        if state == "yielded":
            await self.transition(token, "yield", checkpoint=outcome.checkpoint, retry_at=outcome.retry_at)
        elif state in {"waiting_service", "waiting_external_quota"}:
            # 字段等待恢复期间，保留提供方调用链（prior_call_id/retry_seq）
            # 以及有界拆分检查点。
            await self.transition(token, "checkpoint", checkpoint=outcome.checkpoint, phase=state)
            await self.transition(token, "wait", state=state, retry_at=outcome.retry_at, error=outcome.error)
        elif state == "done":
            semantic = getattr(outcome, "semantic_state", None)
            if semantic is None:
                raise ValueError("done quantum must include semantic_state")
            await self.transition(token, "finish", semantic_state=semantic, checkpoint=outcome.checkpoint)
        elif state == "failed_terminal":
            # Failure carries durable page progress and diagnostics too. A
            # retry must resume that checkpoint rather than repeat paid calls.
            await self.transition(token, "checkpoint", checkpoint=outcome.checkpoint, phase="failed")
            await self.transition(token, "fail", reason=outcome.error or "quantum failed")
        else:
            raise ValueError(f"unsupported quantum outcome: {state}")

    async def transition(self, token: LeaseToken, action: str, **kwargs: Any) -> None:
        job, run = await self._locked(token.job_id)
        try:
            if action == "heartbeat":
                Scheduler.heartbeat(job, run, token, **kwargs)
            elif action == "checkpoint":
                Scheduler.checkpoint(job, run, token, kwargs["checkpoint"], phase=kwargs.get("phase"))
            elif action == "yield":
                Scheduler.yield_quantum(job, run, token, kwargs["checkpoint"], retry_at=kwargs.get("retry_at"))
            elif action == "wait":
                Scheduler.wait(job, run, token, kwargs["state"], retry_at=kwargs.get("retry_at"), error=kwargs.get("error"))
            elif action == "finish":
                Scheduler.finish(job, run, token, semantic_state=kwargs["semantic_state"], checkpoint=kwargs.get("checkpoint"))
            elif action == "fail":
                Scheduler.fail_terminal(job, run, token, kwargs["reason"])
            else:
                raise ValueError(f"unknown field transition: {action}")
            if action == "yield":
                # 唤醒事件在调用方提交前插入，因此消息代理发布丢失时可由发件箱扫描器补偿，
                # 重复发布也不会产生影响。
                await self.enqueue_outbox(
                    kind="field_job_wakeup",
                    aggregate_id=job.job_id,
                    dedupe_key=wakeup_dedupe_key(job.job_id, job.progress_seq),
                    payload={"job_id": str(job.job_id), "run_id": str(run.parse_run_id),
                             "execution_generation": run.execution_generation},
                )
        except Exception as exc:
            if isinstance(exc, RuntimeError) and any(word in str(exc) for word in ("lease", "foreign", "expired")):
                raise StaleLeaseError(str(exc)) from exc
            raise

    async def control_run(self, run_id: UUID, action: str, *, retry_failed: bool = False) -> str:
        run = await self.db.scalar(select(ParseRun).where(ParseRun.parse_run_id == run_id).with_for_update()
                                   .execution_options(populate_existing=True))
        if run is None:
            raise KeyError(f"run {run_id} not found")
        if action not in {"pause", "cancel", "supersede", "resume"}:
            raise ValueError(action)
        if action == "resume":
            if run.status in {"CANCELLED", "SUPERSEDED", "COMPLETED", "COMPLETED_WITH_REVIEW"}:
                raise ValueError("terminal run cannot resume")
            if run.status == "FAILED" and not retry_failed:
                raise ValueError("failed run requires explicit retry policy")
            if run.status in {"PLANNED", "PENDING", "RUNNING", "YIELDED"}:
                return run.status
            run.status = "RUNNING"
        else:
            target = {"pause": "PAUSED", "cancel": "CANCELLED", "supersede": "SUPERSEDED"}[action]
            if run.status in {"COMPLETED", "COMPLETED_WITH_REVIEW", "FAILED", "CANCELLED", "SUPERSEDED"}:
                raise ValueError(f"run {run.status} is terminal")
            run.status = target
        run.execution_generation += 1
        jobs = (await self.db.execute(select(ParseFieldJob).where(ParseFieldJob.run_id == run_id).with_for_update())).scalars().all()
        for job in jobs:
            job.fence_token += 1
            job.lease_owner = None
            job.lease_until = None
            if action == "pause" and job.execution_state in ACTIVE_FIELD_STATES:
                job.execution_state = "paused"
            elif action == "cancel" and job.execution_state in ACTIVE_FIELD_STATES | {"paused"}:
                job.execution_state = "cancelled"
            elif action == "supersede" and job.execution_state in ACTIVE_FIELD_STATES | {"paused"}:
                job.execution_state = "superseded"
            elif action == "resume" and job.execution_state in {"paused", "running", "yielded"}:
                job.execution_state = "queued"
                job.next_attempt_at = None
            elif action == "resume" and job.execution_state in {"waiting_service", "waiting_external_quota"}:
                # 显式恢复也是管理员在提供方配额或熔断决策后的放行操作。
                # P07 创建新的调用链；P05 只负责让持久化字段重新可运行。
                job.execution_state = "queued"
                job.next_attempt_at = None
            elif action == "resume" and retry_failed and job.execution_state == "failed_terminal":
                # 重试是在同一冻结任务上的显式新尝试；下次领取时会递增栅栏和尝试计数。
                job.execution_state = "queued"
                job.semantic_state = "unknown"
                job.next_attempt_at = None
                job.last_error = None
                if (run.extraction_profile or {}).get("extraction_strategy") == "page_batch_v1":
                    checkpoint = dict(job.checkpoint or {})
                    # Deliberate resume grants a new bounded processing window;
                    # successful page results and their ledger remain intact.
                    checkpoint["page_started_at"] = datetime.utcnow().isoformat()
                    failed_units = set(checkpoint.get("failed_units") or [])
                    units = list(checkpoint.get("page_units") or [])
                    if failed_units:
                        checkpoint["page_results"] = [result for result in checkpoint.get("page_results", [])
                            if (result.get("unit") or {}).get("unit_id") not in failed_units]
                        checkpoint["page_index"] = min(
                            (index for index, unit in enumerate(units) if unit.get("unit_id") in failed_units),
                            default=int(checkpoint.get("page_index", 0)))
                        checkpoint["failed_units"] = []
                        generations = dict(checkpoint.get("unit_retry_generations") or {})
                        for unit_id in failed_units:
                            generations[unit_id] = int(generations.get(unit_id, 0)) + 1
                        checkpoint["unit_retry_generations"] = generations
                        checkpoint["page_retry"] = 0
                    job.checkpoint = checkpoint
            if action == "resume" and job.execution_state == "queued":
                await self.enqueue_outbox(kind="field_job_wakeup", aggregate_id=job.job_id,
                                          dedupe_key=f"{job.job_id}:resume:{run.execution_generation}",
                                          payload={"job_id": str(job.job_id), "run_id": str(run_id)})
        return run.status

    async def aggregate(self, run_id: UUID) -> str:
        run = await self.db.scalar(select(ParseRun).where(ParseRun.parse_run_id == run_id)
                                   .with_for_update().execution_options(populate_existing=True))
        if run is None:
            raise KeyError(run_id)
        jobs = (await self.db.execute(select(ParseFieldJob).where(ParseFieldJob.run_id == run_id))).scalars().all()
        status = aggregate_run_status(run, list(jobs))
        if status != run.status and run.status not in {"PAUSED", "CANCELLED", "SUPERSEDED"}:
            run.status = status
        if run.status in _COUNTED_RUN_STATUSES:
            await write_extracted_field_counts(self.db, [run])
        if status in {"COMPLETED", "COMPLETED_WITH_REVIEW", "FAILED"} and run.completed_at is None:
            run.completed_at = datetime.utcnow()
            if run.started_at is not None:
                run.duration_seconds = (run.completed_at - run.started_at).total_seconds()
        return status

    async def consume_run_wakeup(self, run_id: UUID, event_id: UUID, *,
                                 dispatch_attempt: int | None = None, task_id: str | None = None) -> bool:
        """工作进程只能确认启动它的那条确切代理唤醒事件。"""
        run = await self.db.scalar(select(ParseRun).where(ParseRun.parse_run_id == run_id)
                                  .with_for_update().execution_options(populate_existing=True))
        if run is None or run.status not in ACTIVE_RUN_STATES:
            return False
        event = await self.db.get(ExecutionOutbox, event_id, with_for_update=True, populate_existing=True)
        if (event is None or event.kind != "field_job_wakeup" or
                str((event.payload or {}).get("run_id")) != str(run_id) or
                not (event.payload or {}).get("dispatch_outstanding")):
            return False
        if dispatch_attempt is not None and dispatch_attempt != event.attempts:
            return False
        if (event.payload or {}).get("execution_generation") != run.execution_generation:
            return False
        event.payload = {**dict(event.payload), "dispatch_outstanding": False,
                         "received_at": datetime.utcnow().isoformat(), "received_task_id": task_id}
        if event.dispatch_state == "failed":
            event.dispatch_state = "sent"
        event.next_attempt_at = None
        event.updated_at = datetime.utcnow()
        return True

    async def enqueue_outbox(self, *, kind: str, aggregate_id: UUID, dedupe_key: str, payload: dict[str, Any]) -> ExecutionOutbox:
        statement = insert(ExecutionOutbox).values(kind=kind, aggregate_id=aggregate_id,
                    dedupe_key=dedupe_key, payload=payload, dispatch_state="pending")
        await self.db.execute(statement.on_conflict_do_nothing(constraint="uq_execution_outbox_kind_dedupe"))
        event = await self.db.scalar(select(ExecutionOutbox).where(ExecutionOutbox.kind == kind,
                                    ExecutionOutbox.dedupe_key == dedupe_key))
        assert event is not None
        return event
