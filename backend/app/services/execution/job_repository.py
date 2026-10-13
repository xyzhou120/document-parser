"""Async SQLAlchemy repository for field execution jobs.

The repository never commits a transaction.  Callers can therefore atomically
persist a checkpoint and an outbox event in the same transaction.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import ParseRun
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


class JobRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def initialize_jobs(self, run_id: UUID, *, revision_id: UUID, fields: list[dict[str, Any]]) -> list[ParseFieldJob]:
        """Create the immutable field-job denominator for a run idempotently.

        ``fields`` must already be a frozen rule snapshot.  This method never
        reads the current rule configuration and never deletes an existing
        field job, which makes retries safe after a DB/message boundary.
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
        # A single run->job lock order is shared by claim, publication, control
        # and recovery.  populate_existing prevents a long-lived session from
        # accepting a cached generation/fence after it waited on a lock.
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
        runnable = select(ParseFieldJob.job_id).where(ParseFieldJob.run_id == ParseRun.parse_run_id, *ready).exists()
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
        job = await self.db.scalar(query.execution_options(populate_existing=True))
        if job is None:
            return None
        run.status = "RUNNING"
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
        """Atomically guard and persist domain results, checkpoint and dispatch."""
        job, run = await self._locked(token.job_id)
        require_lease(job, run, token)
        if writer is not None:
            await writer(self.db, job, outcome)
        state = getattr(outcome, "state", None)
        if state == "yielded":
            await self.transition(token, "yield", checkpoint=outcome.checkpoint, retry_at=outcome.retry_at)
        elif state in {"waiting_service", "waiting_external_quota"}:
            # Preserve provider call lineage (prior_call_id/retry_seq) and any
            # bounded split checkpoint while the field waits for recovery.
            await self.transition(token, "checkpoint", checkpoint=outcome.checkpoint, phase=state)
            await self.transition(token, "wait", state=state, retry_at=outcome.retry_at, error=outcome.error)
        elif state == "done":
            semantic = getattr(outcome, "semantic_state", None)
            if semantic is None:
                raise ValueError("done quantum must include semantic_state")
            await self.transition(token, "finish", semantic_state=semantic, checkpoint=outcome.checkpoint)
        elif state == "failed_terminal":
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
                # The wake-up event is inserted before the caller commits.  A
                # lost broker publish is therefore repaired by the outbox
                # scanner, while a duplicate publish is harmless.
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
                # Explicit resume is also the administrator's release after a
                # provider quota/circuit decision.  P07 creates a fresh call
                # lineage; P05 only makes the durable field runnable.
                job.execution_state = "queued"
                job.next_attempt_at = None
            elif action == "resume" and retry_failed and job.execution_state == "failed_terminal":
                # A retry is an explicit new attempt on the same frozen job;
                # the next claim raises its fence and attempt counters.
                job.execution_state = "queued"
                job.semantic_state = "unknown"
                job.next_attempt_at = None
                job.last_error = None
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
        return status

    async def consume_run_wakeup(self, run_id: UUID, event_id: UUID, *,
                                 dispatch_attempt: int | None = None, task_id: str | None = None) -> bool:
        """A worker acknowledges only the exact broker wake that started it."""
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

