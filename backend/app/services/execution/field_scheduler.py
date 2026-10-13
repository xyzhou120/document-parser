"""Fair, resumable field scheduling state machine.

This module owns field-level control state.  It does not know how a model is
called or how candidates are produced.  Every mutation that can publish work
is guarded by a :class:`LeaseToken`; an old worker therefore gets a
``StaleLeaseError`` instead of silently overwriting a newer attempt.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from app.models.database import ParseRun
from app.models.execution import ParseFieldJob

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
    """Stable idempotency key for one persisted progress transition."""
    return f"{job_id}:{progress_seq}"


class FieldExecutionError(RuntimeError):
    """A worker attempted an invalid field transition."""


class StaleLeaseError(FieldExecutionError):
    """A lease expired or was revoked by takeover or run control."""


class RunControlError(RuntimeError):
    """A run control operation is not valid for its current state."""


@dataclass(frozen=True, slots=True)
class LeaseToken:
    """Capabilities captured when a worker claims a job."""

    run_id: UUID
    job_id: UUID
    owner: str
    execution_generation: int
    fence_token: int


@dataclass(slots=True)
class QuantumOutcome:
    """Minimal P13 runner result; model/retrieval details stay outside P05."""

    state: str
    checkpoint: dict[str, Any]
    semantic_state: str | None = None
    retry_at: datetime | None = None
    error: str | None = None


QuantumExecutor = Callable[[LeaseToken, dict[str, Any]], Awaitable[QuantumOutcome]]


class FieldSchedulerRunner:
    """Claim -> external quantum -> short guarded result transaction.

    ``sessions`` is an async_sessionmaker (or a compatible injected factory).
    The result writer may persist P06 candidate decisions and P10 validated
    facts; it runs only after the lease guard and in the same transaction as
    the checkpoint and wake-up event.  Provider billing is independently
    persisted by P07, including when this result loses its lease.
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
        # No SQL transaction is held across a model/OCR request.
        outcome = await execute(token, checkpoint)
        async with self.sessions() as db, db.begin():
            repository = JobRepository(db)
            if before_publish is not None:
                handled = await before_publish(db, token, outcome)
                if handled:
                    return token
            await repository.publish(token, outcome, writer=write_result)
            await repository.aggregate(token.run_id)
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
    """Pure transition helper used by ``JobRepository`` and deterministic tests."""

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
        """Persist progress and give the quantum back to the scheduler.

        ``yielded`` is an observable internal state.  The due scanner changes
        it back to ``queued`` without user interaction.
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
    """Return the canonical control-first aggregate status for a run."""
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
