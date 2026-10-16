"""公平且可恢复的字段调度状态机。

本模块负责字段级控制状态，不关心模型调用方式或候选项生成方式。
所有可能发布工作的变更都受 LeaseToken 保护，因此旧工作进程会收到
而不是静默覆盖更新的尝试，并抛出 StaleLeaseError。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from app.models.database import ParseRun
from app.models.execution import ParseFieldJob

logger = logging.getLogger(__name__)


def _exception_outcome(exc: Exception, checkpoint: dict) -> QuantumOutcome:
    from app.services.errors import is_retryable_error
    checkpoint = deepcopy(checkpoint)
    failures = int(checkpoint.get("infrastructure_failures", 0)) + 1
    checkpoint["infrastructure_failures"] = failures
    retryable = is_retryable_error(exc) and failures <= 6
    return QuantumOutcome(
        "waiting_service" if retryable else "failed_terminal", checkpoint,
        retry_at=datetime.utcnow() + timedelta(seconds=min(300, 15 * 2 ** min(failures - 1, 5))) if retryable else None,
        error=f"quantum_exception:{type(exc).__name__}:attempt:{failures}",
    )

ACTIVE_RUN_STATES = {
    "PLANNED",
    "PENDING",
    "RUNNING",
    "YIELDED",
    "PARTIAL",
    "WAITING_SERVICE",
    "WAITING_EXTERNAL_QUOTA",
}
TERMINAL_RUN_STATES = {"COMPLETED", "COMPLETED_WITH_REVIEW", "FAILED", "CANCELLED", "SUPERSEDED"}
ACTIVE_FIELD_STATES = {"queued", "running", "yielded", "waiting_service", "waiting_external_quota"}
TERMINAL_FIELD_STATES = {"done", "failed_terminal", "cancelled", "superseded"}


def wakeup_dedupe_key(job_id: UUID, progress_seq: int) -> str:
    """一次持久化进度转换的稳定幂等键。"""
    return f"{job_id}:{progress_seq}"


class FieldExecutionError(RuntimeError):
    """工作进程尝试了无效的字段状态转换。"""


class StaleLeaseError(FieldExecutionError):
    """租约已过期，或因接管、运行控制而被撤销。"""


class RunControlError(RuntimeError):
    """运行控制操作对于当前状态无效。"""


@dataclass(frozen=True, slots=True)
class LeaseToken:
    """工作进程领取任务时记录的能力信息。"""

    run_id: UUID
    job_id: UUID
    owner: str
    execution_generation: int
    fence_token: int


@dataclass(slots=True)
class QuantumOutcome:
    """最小 P13 运行结果；模型和检索细节留在 P05 之外。"""

    state: str
    checkpoint: dict[str, Any]
    semantic_state: str | None = None
    retry_at: datetime | None = None
    error: str | None = None


QuantumExecutor = Callable[[LeaseToken, dict[str, Any]], Awaitable[QuantumOutcome]]


class FieldSchedulerRunner:
    """领取任务 -> 执行外部量子 -> 短事务保护结果。

        sessions 是 async_sessionmaker（或兼容的注入工厂）。
        结果写入方可以持久化 P06 候选决策和 P10 已校验事实；它只在租约防护后
        运行，并与检查点和唤醒事件处于同一事务。即使本结果失去租约，
        P07 仍会独立持久化提供方计费信息。
    """

    def __init__(self, sessions: Any):
        self.sessions = sessions

    async def run_next(self, *, run_id: UUID | None, owner: str, execute: QuantumExecutor,
                       write_result: Any = None, before_publish: Any = None,
                       lease_seconds: int = 90) -> LeaseToken | None:
        from .job_repository import JobRepository

        async with self.sessions() as db, db.begin():
            token = await JobRepository(db).claim_next(run_id=run_id, owner=owner, lease_seconds=lease_seconds)
            if token is None:
                return None
            job = await db.get(ParseFieldJob, token.job_id)
            checkpoint = deepcopy(job.checkpoint or {})
        # 模型或 OCR 请求期间不持有 SQL 事务，但不能让租约在外部调用期间自然过期。
        # 心跳使用独立的短事务，避免把长模型调用和数据库锁绑定在一起。
        async def heartbeat_loop():
            interval = max(1.0, min(30.0, lease_seconds / 3))
            while True:
                await asyncio.sleep(interval)
                try:
                    async with self.sessions() as heartbeat_db, heartbeat_db.begin():
                        await JobRepository(heartbeat_db).transition(
                            token, "heartbeat", lease_seconds=lease_seconds)
                except StaleLeaseError:
                    raise
                except Exception:
                    # 心跳失败时让发布阶段的 fence/lease 校验做最终裁决；
                    # 暂时性数据库故障不能在这里覆盖业务结果。
                    logger.warning("字段续租暂时失败 job_id=%s", token.job_id, exc_info=True)

        started = time.monotonic()
        heartbeat = asyncio.create_task(heartbeat_loop())
        execution = asyncio.create_task(execute(token, checkpoint))
        recovered = False
        try:
            done, _ = await asyncio.wait((heartbeat, execution), return_when=asyncio.FIRST_COMPLETED)
            if execution not in done:
                await heartbeat  # 已失去所有权：停止旧调用，禁止覆盖接管者或取消状态。
            outcome = await execution
            outcome.checkpoint.pop("infrastructure_failures", None)
        except StaleLeaseError:
            raise
        except Exception as exc:
            # 外部依赖异常必须落成持久化状态，不能只让 Celery 看到一个失败任务。
            outcome = _exception_outcome(exc, checkpoint)
            recovered = True
            logger.exception("字段量子执行失败 run_id=%s job_id=%s", token.run_id, token.job_id)
        finally:
            for task in (heartbeat, execution):
                if not task.done():
                    task.cancel()
            await asyncio.gather(heartbeat, execution, return_exceptions=True)
        execution_seconds = time.monotonic() - started

        async def persist(writer):
            async with self.sessions() as db, db.begin():
                repository = JobRepository(db)
                if before_publish is not None and not recovered:
                    handled = await before_publish(db, token, outcome)
                    if handled:
                        return
                await repository.publish(token, outcome, writer=writer, lease_seconds=lease_seconds)
                await repository.aggregate(token.run_id)

        try:
            await persist(None if recovered else write_result)
        except StaleLeaseError:
            raise
        except Exception as exc:
            # 领域 writer 的确定性异常也必须结束；先回滚结果事务，再用原检查点
            # 保存失败。数据库本身仍不可用时抛给 Celery 重试/租约恢复。
            logger.exception("字段量子发布失败 run_id=%s job_id=%s", token.run_id, token.job_id)
            outcome = _exception_outcome(exc, checkpoint)
            recovered = True
            await persist(None)
        logger.info("字段量子完成 run_id=%s job_id=%s fence=%s state=%s execute_seconds=%.3f publish_seconds=%.3f",
                    token.run_id, token.job_id, token.fence_token, outcome.state, execution_seconds,
                    time.monotonic() - started - execution_seconds)
        return token


def _now(value: datetime | None = None) -> datetime:
    return value or datetime.utcnow()


def _token_matches(job: ParseFieldJob, run: ParseRun, token: LeaseToken) -> bool:
    return (
        job.job_id == token.job_id
        and run.parse_run_id == token.run_id
        and run.execution_generation == token.execution_generation
        and job.fence_token == token.fence_token
        and job.lease_owner == token.owner
    )


def require_lease(job: ParseFieldJob, run: ParseRun, token: LeaseToken, *, now: datetime | None = None) -> None:
    if not _token_matches(job, run, token):
        raise StaleLeaseError("stale or foreign field lease")
    if run.status not in ACTIVE_RUN_STATES or job.execution_state != "running":
        raise StaleLeaseError("lease is no longer active")
    if job.lease_until is None or job.lease_until <= _now(now):
        raise StaleLeaseError("field lease expired")


class Scheduler:
    """供 JobRepository 和确定性测试使用的纯转换辅助函数。"""

    @staticmethod
    def claim(job: ParseFieldJob, run: ParseRun, owner: str, *, now: datetime | None = None,
              lease_seconds: int = 90) -> LeaseToken:
        now = _now(now)
        if not owner or lease_seconds <= 0:
            raise ValueError("owner and positive lease_seconds are required")
        if run.status not in ACTIVE_RUN_STATES:
            raise FieldExecutionError(f"run {run.status} cannot claim work")
        if job.execution_state not in {"queued", "yielded"}:
            raise FieldExecutionError(f"job {job.execution_state} is not claimable")
        if job.next_attempt_at and job.next_attempt_at > now:
            raise FieldExecutionError("job backoff has not elapsed")
        job.fence_token += 1
        job.attempt += 1
        job.execution_state = "running"
        job.phase = "running"
        job.lease_owner = owner
        job.lease_until = now + timedelta(seconds=max(1, lease_seconds))
        job.next_attempt_at = None
        return LeaseToken(run.parse_run_id, job.job_id, owner, run.execution_generation, job.fence_token)

    @staticmethod
    def heartbeat(job: ParseFieldJob, run: ParseRun, token: LeaseToken, *, now: datetime | None = None,
                  lease_seconds: int = 90) -> None:
        require_lease(job, run, token, now=now)
        job.lease_until = _now(now) + timedelta(seconds=max(1, lease_seconds))

    @staticmethod
    def checkpoint(job: ParseFieldJob, run: ParseRun, token: LeaseToken, checkpoint: dict[str, Any],
                   *, phase: str | None = None, now: datetime | None = None) -> None:
        require_lease(job, run, token, now=now)
        job.checkpoint = dict(checkpoint)
        if phase:
            job.phase = phase
        job.progress_seq += 1

    @staticmethod
    def yield_quantum(job: ParseFieldJob, run: ParseRun, token: LeaseToken, checkpoint: dict[str, Any],
                      *, retry_at: datetime | None = None, now: datetime | None = None) -> None:
        """持久化进度，并将本次量子执行权交还调度器。

        yielded 是可观测的内部状态。到期扫描器无需用户操作即可将其改回 queued。
        """
        require_lease(job, run, token, now=now)
        job.checkpoint = dict(checkpoint)
        job.progress_seq += 1
        job.phase = "yielded"
        job.execution_state = "yielded"
        job.next_attempt_at = retry_at or _now(now)
        job.lease_owner = None
        job.lease_until = None

    @staticmethod
    def wait(job: ParseFieldJob, run: ParseRun, token: LeaseToken, state: str, *, retry_at: datetime | None,
             error: str | None = None, now: datetime | None = None) -> None:
        require_lease(job, run, token, now=now)
        if state not in {"waiting_service", "waiting_external_quota"}:
            raise ValueError("wait state must identify service or external quota")
        job.execution_state = state
        job.phase = state
        job.next_attempt_at = retry_at
        job.last_error = error
        job.lease_owner = None
        job.lease_until = None
        job.progress_seq += 1

    @staticmethod
    def finish(job: ParseFieldJob, run: ParseRun, token: LeaseToken, *, semantic_state: str,
               checkpoint: dict[str, Any] | None = None, now: datetime | None = None) -> None:
        require_lease(job, run, token, now=now)
        if semantic_state not in {"completed", "review_required", "partial_candidates", "not_found", "incomplete"}:
            raise ValueError(f"invalid semantic state: {semantic_state}")
        if semantic_state in {"partial_candidates", "incomplete"}:
            raise FieldExecutionError("partial or incomplete work cannot be marked done")
        job.semantic_state = semantic_state
        job.execution_state = "done"
        job.phase = "finished"
        if checkpoint is not None:
            job.checkpoint = dict(checkpoint)
        job.progress_seq += 1
        job.lease_owner = None
        job.lease_until = None
        job.next_attempt_at = None

    @staticmethod
    def fail_terminal(job: ParseFieldJob, run: ParseRun, token: LeaseToken, reason: str, *, now: datetime | None = None) -> None:
        require_lease(job, run, token, now=now)
        job.execution_state = "failed_terminal"
        job.semantic_state = "incomplete"
        job.phase = "failed"
        job.last_error = reason
        job.progress_seq += 1
        job.lease_owner = None
        job.lease_until = None


def aggregate_run_status(run: ParseRun, jobs: list[ParseFieldJob]) -> str:
    """返回以控制状态优先的运行聚合状态。"""
    if run.status in {"PAUSED", "CANCELLED", "SUPERSEDED"}:
        return run.status
    if not jobs:
        return "PLANNED" if run.status == "PLANNED" else run.status
    if any(job.execution_state == "waiting_external_quota" for job in jobs):
        return "WAITING_EXTERNAL_QUOTA"
    if any(job.execution_state == "waiting_service" for job in jobs):
        return "WAITING_SERVICE"
    if any(job.execution_state == "yielded" for job in jobs) and not any(
        job.execution_state in {"queued", "running"} for job in jobs
    ):
        return "YIELDED"
    if any(job.execution_state in {"queued", "running"} for job in jobs):
        return "RUNNING"
    if any(job.execution_state == "failed_terminal" or job.semantic_state == "incomplete" for job in jobs):
        return "FAILED"
    if any(job.semantic_state in {"partial_candidates"} for job in jobs):
        return "PARTIAL"
    if all(job.execution_state == "done" and job.semantic_state in {"completed", "not_found"} for job in jobs):
        return "COMPLETED"
    if all(job.execution_state == "done" for job in jobs) and any(job.semantic_state == "review_required" for job in jobs):
        return "COMPLETED_WITH_REVIEW"
    return "PARTIAL"
