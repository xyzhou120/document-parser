"""Short-transaction call accounting; field result publication belongs to the scheduler.

``reservation.gateway_outcome`` keeps the transport result for crash/re-delivery recovery.
It is not a published fact. Late usage can settle an old fence without changing field state.
"""

import asyncio
import hashlib
import json
import uuid
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from app.models.database import ParseRun
from app.models.execution import ParseFieldJob, ParseModelCall


class AdminHardLimitExceeded(RuntimeError):
    """The frozen administrator cost ceiling rejected a new real attempt."""

    def __init__(self, limit: int, accounted: int, requested: int):
        self.limit, self.accounted, self.requested = limit, accounted, requested
        super().__init__(f"admin_hard_limit:{accounted}+{requested}>{limit}")


def token_total(value: dict | None) -> int:
    value = value or {}
    return max(0, int(value.get("total_tokens") or
                      int(value.get("prompt_tokens") or 0) +
                      int(value.get("completion_tokens") or 0)))


def accounted_tokens(call: "CallRecord") -> int:
    """Unknown charges keep the full reserved upper bound, including after expiry."""
    if call.state == "queued":
        return 0
    if call.charge_state in {"actual", "reconciled"}:
        return token_total(call.usage)
    return max(token_total(call.reservation), token_total(call.usage))


@dataclass
class CallRecord:
    call_id: uuid.UUID
    job_id: uuid.UUID
    fence: int
    provider_key: str
    dispatch_key: str
    checkpoint_key: str
    retry_seq: int
    retry_of_call_id: uuid.UUID | None
    candidate_set_hash: str
    split_path: list
    reservation: dict
    state: str = "queued"
    charge_state: str = "reserved"
    usage: dict | None = None
    usage_revision: int = 0
    unknown_deadline: datetime | None = None
    usage_quality: str | None = None
    finish_reason: str | None = None
    provider_request_id: str | None = None
    created_at: datetime | None = None
    sent_at: datetime | None = None
    completed_at: datetime | None = None


def call_record(row: Any) -> CallRecord:
    return CallRecord(**{name: deepcopy(getattr(row, name))
                         for name in CallRecord.__dataclass_fields__})


def new_call(*, job_id, fence, provider_key, dispatch_key, checkpoint_key,
             candidate_ids, reservation, retry_seq=0, retry_of_call_id=None,
             split_path=(), now=None) -> CallRecord:
    if retry_seq < 0 or fence < 0 or (retry_seq > 0 and retry_of_call_id is None):
        raise ValueError("invalid retry lineage or fence")
    digest = hashlib.sha256(json.dumps(list(candidate_ids), separators=(",", ":")).encode()).hexdigest()
    return CallRecord(uuid.uuid4(), job_id, fence, provider_key, dispatch_key, checkpoint_key,
                      retry_seq, retry_of_call_id, digest, list(split_path), deepcopy(reservation),
                      created_at=now or datetime.utcnow())


def same_dispatch(old: CallRecord, new: CallRecord) -> None:
    for name in ("job_id", "fence", "provider_key", "checkpoint_key", "retry_seq",
                 "retry_of_call_id", "candidate_set_hash", "split_path"):
        if getattr(old, name) != getattr(new, name):
            raise ValueError("dispatch_key reused for a different request")
    if old.reservation.get("request_hash") != new.reservation.get("request_hash"):
        raise ValueError("dispatch_key reused for a different model input")


def finish_record(call: Any, *, state, usage=None, outcome=None, finish_reason=None,
                  provider_request_id=None, now=None, unknown_seconds=300) -> None:
    if state not in {"succeeded", "length", "invalid", "timeout", "uncertain"}:
        raise ValueError("invalid final call state")
    now = now or datetime.utcnow()
    call.state, call.completed_at = state, now
    call.finish_reason, call.provider_request_id = finish_reason, provider_request_id
    if usage is not None:
        call.usage, call.usage_revision = deepcopy(usage), call.usage_revision + 1
        call.usage_quality, call.charge_state, call.unknown_deadline = "actual", "actual", None
    elif call.charge_state not in {"actual", "reconciled"}:
        call.usage_quality, call.charge_state = "uncertain", "uncertain"
        call.unknown_deadline = now + timedelta(seconds=unknown_seconds)
    if outcome is not None:
        call.reservation = {**call.reservation, "gateway_outcome": deepcopy(outcome)}


def reconcile_record(call: Any, usage: dict, revision: int) -> bool:
    if revision <= call.usage_revision:
        return False
    call.usage, call.usage_revision = deepcopy(usage), revision
    call.usage_quality, call.charge_state, call.unknown_deadline = "actual", "reconciled", None
    return True


class CallLedger:
    """Production PostgreSQL ledger. Each method owns and closes a short transaction."""

    def __init__(self, session_factory, *, unknown_seconds=300):
        self.sessions, self.unknown_seconds = session_factory, unknown_seconds

    async def reserve(self, record: CallRecord) -> tuple[CallRecord, bool]:
        values = {name: deepcopy(getattr(record, name)) for name in CallRecord.__dataclass_fields__}
        async with self.sessions() as db, db.begin():
            result = await db.execute(insert(ParseModelCall).values(**values)
                                      .on_conflict_do_nothing(index_elements=["dispatch_key"])
                                      .returning(ParseModelCall.call_id))
            created = result.scalar_one_or_none() is not None
            row = (await db.execute(select(ParseModelCall).where(
                ParseModelCall.dispatch_key == record.dispatch_key))).scalar_one()
            snapshot = call_record(row)
            same_dispatch(snapshot, record)
            return snapshot, created

    async def get(self, call_id) -> CallRecord:
        async with self.sessions() as db:
            row = await db.get(ParseModelCall, call_id)
            if row is None:
                raise KeyError(call_id)
            return call_record(row)

    async def claim(self, call_id, *, now=None, hard_limit_tokens: int | None = None) -> bool:
        now = now or datetime.utcnow()
        async with self.sessions() as db, db.begin():
            call = await db.get(ParseModelCall, call_id)
            if call is None:
                raise KeyError(call_id)
            # Always use run -> call lock order, including unlimited attempts, so
            # one caller cannot bypass a concurrent run cost check.
            run = (await db.execute(select(ParseRun).join(
                ParseFieldJob, ParseFieldJob.run_id == ParseRun.parse_run_id
            ).where(ParseFieldJob.job_id == call.job_id).with_for_update(of=ParseRun))).scalar_one_or_none()
            if run is None:
                raise KeyError(f"run for call {call_id} does not exist")
            call = await db.get(ParseModelCall, call_id, with_for_update=True, populate_existing=True)
            if call.state != "queued":
                return False
            profile = run.extraction_profile or {}
            if "hard_limit_tokens" in profile:
                hard_limit_tokens = profile["hard_limit_tokens"]
            if hard_limit_tokens is not None:
                if isinstance(hard_limit_tokens, bool) or not isinstance(hard_limit_tokens, int) or hard_limit_tokens <= 0:
                    raise ValueError("hard_limit_tokens must be a positive integer or None")
                rows = (await db.execute(select(ParseModelCall).join(
                    ParseFieldJob, ParseFieldJob.job_id == ParseModelCall.job_id).where(
                    ParseFieldJob.run_id == run.parse_run_id))).scalars().all()
                accounted = sum(accounted_tokens(call_record(row)) for row in rows)
                requested = token_total(call.reservation)
                if accounted + requested > hard_limit_tokens:
                    raise AdminHardLimitExceeded(hard_limit_tokens, accounted, requested)
            result = await db.execute(update(ParseModelCall).where(
                ParseModelCall.call_id == call_id, ParseModelCall.state == "queued"
            ).values(state="sending", sent_at=now,
                     unknown_deadline=now + timedelta(seconds=self.unknown_seconds)))
            return bool(result.rowcount == 1)

    async def record_wait(self, call_id, outcome: dict) -> None:
        """Unsent administrator waits are durable but never count as a charge."""
        async with self.sessions() as db, db.begin():
            row = await db.get(ParseModelCall, call_id, with_for_update=True)
            if row is not None and row.state == "queued":
                row.reservation = {**(row.reservation or {}), "gateway_outcome": deepcopy(outcome)}

    async def started(self, call_id) -> None:
        async with self.sessions() as db, db.begin():
            await db.execute(update(ParseModelCall).where(
                ParseModelCall.call_id == call_id, ParseModelCall.state == "sending"
            ).values(state="started"))

    async def finish(self, call_id, **kwargs) -> CallRecord:
        async with self.sessions() as db, db.begin():
            row = (await db.execute(select(ParseModelCall).where(
                ParseModelCall.call_id == call_id).with_for_update())).scalar_one()
            finish_record(row, unknown_seconds=self.unknown_seconds, **kwargs)
            return call_record(row)

    async def reconcile(self, call_id, usage: dict, revision: int) -> bool:
        async with self.sessions() as db, db.begin():
            row = (await db.execute(select(ParseModelCall).where(
                ParseModelCall.call_id == call_id).with_for_update())).scalar_one()
            return reconcile_record(row, usage, revision)

    async def expire_unknown(self, *, now=None) -> list[CallRecord]:
        now = now or datetime.utcnow()
        async with self.sessions() as db, db.begin():
            rows = (await db.execute(select(ParseModelCall).where(
                ParseModelCall.unknown_deadline <= now,
                ParseModelCall.charge_state.in_(["reserved", "uncertain"])
            ).with_for_update(skip_locked=True))).scalars().all()
            for row in rows:
                if row.state in {"sending", "started"}:
                    row.state, row.completed_at = "uncertain", now
                row.charge_state, row.usage_quality = "uncertain", "estimated"
                # Expiry permits recovery; it never erases the potential charge.
                row.unknown_deadline = None
            return [call_record(row) for row in rows]

    async def usage_totals(self, job_id) -> dict:
        async with self.sessions() as db:
            rows = (await db.execute(select(ParseModelCall).where(
                ParseModelCall.job_id == job_id))).scalars().all()
            return totals([call_record(row) for row in rows])


def totals(records: list[CallRecord]) -> dict:
    return {"calls": len(records), "actual_tokens": sum(token_total(r.usage) for r in records),
            "accounted_tokens": sum(accounted_tokens(r) for r in records),
            "uncertain_calls": sum(r.charge_state == "uncertain" for r in records)}


class MemoryCallLedger:
    """Deterministic fake ledger for tests; never a production persistence fallback."""

    def __init__(self, *, unknown_seconds=300):
        self.records, self.dispatches = {}, {}
        self.lock, self.unknown_seconds = asyncio.Lock(), unknown_seconds
        self.job_runs = {}

    def register_job(self, job_id, run_id):
        self.job_runs[job_id] = run_id

    async def reserve(self, record):
        async with self.lock:
            prior = self.dispatches.get(record.dispatch_key)
            if prior:
                old = self.records[prior]
                same_dispatch(old, record)
                return deepcopy(old), False
            self.records[record.call_id] = deepcopy(record)
            self.dispatches[record.dispatch_key] = record.call_id
            return deepcopy(record), True

    async def get(self, call_id):
        return deepcopy(self.records[call_id])

    async def claim(self, call_id, *, now=None, hard_limit_tokens: int | None = None):
        async with self.lock:
            row = self.records[call_id]
            if row.state != "queued":
                return False
            if hard_limit_tokens is not None:
                if isinstance(hard_limit_tokens, bool) or not isinstance(hard_limit_tokens, int) or hard_limit_tokens <= 0:
                    raise ValueError("hard_limit_tokens must be a positive integer or None")
                run_id = self.job_runs.get(row.job_id, row.job_id)
                accounted = sum(accounted_tokens(item) for item in self.records.values()
                                if self.job_runs.get(item.job_id, item.job_id) == run_id)
                requested = token_total(row.reservation)
                if accounted + requested > hard_limit_tokens:
                    raise AdminHardLimitExceeded(hard_limit_tokens, accounted, requested)
            row.state, row.sent_at = "sending", now or datetime.utcnow()
            row.unknown_deadline = row.sent_at + timedelta(seconds=self.unknown_seconds)
            return True

    async def record_wait(self, call_id, outcome):
        async with self.lock:
            row = self.records[call_id]
            if row.state == "queued":
                row.reservation = {**row.reservation, "gateway_outcome": deepcopy(outcome)}

    async def started(self, call_id):
        async with self.lock:
            if self.records[call_id].state == "sending":
                self.records[call_id].state = "started"

    async def finish(self, call_id, **kwargs):
        async with self.lock:
            finish_record(self.records[call_id], unknown_seconds=self.unknown_seconds, **kwargs)
            return deepcopy(self.records[call_id])

    async def reconcile(self, call_id, usage, revision):
        async with self.lock:
            return reconcile_record(self.records[call_id], usage, revision)

    async def expire_unknown(self, *, now=None):
        now, expired = now or datetime.utcnow(), []
        async with self.lock:
            for row in self.records.values():
                if row.unknown_deadline and row.unknown_deadline <= now:
                    if row.state in {"sending", "started"}:
                        row.state, row.completed_at = "uncertain", now
                    row.charge_state, row.usage_quality = "uncertain", "estimated"
                    row.unknown_deadline = None
                    expired.append(deepcopy(row))
        return expired

    async def usage_totals(self, job_id):
        return totals([r for r in self.records.values() if r.job_id == job_id])
