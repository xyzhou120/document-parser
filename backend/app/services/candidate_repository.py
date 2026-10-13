"""Persistence adapter for P06 range cursors and candidates.

Methods deliberately never commit.  The caller owns the transaction so candidate upsert and
range cursor advancement can be committed atomically by the execution scheduler.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable, Sequence
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.execution import ParseCandidate, ParseFieldJob, ParseRangeWorkItem
from app.services.candidate_retrieval import Candidate
from app.services.retrieval_plan import ScanCursor

ALL_STATES = frozenset({"pending", "in_flight", "accepted", "rejected", "duplicate", "unresolved"})


def validate_scan_batch(work: Any, batch: Any, revision_id: Any) -> None:
    """Reject an unbound/out-of-range checkpoint before any candidate writes."""
    cursor = batch.cursor.as_dict() if hasattr(batch.cursor, "as_dict") else dict(batch.cursor)
    if str(cursor.get("revision") or "") != str(revision_id):
        raise ValueError("scan cursor revision does not belong to field job")
    if cursor.get("range_key") != work.range_key:
        raise ValueError("scan cursor range does not belong to work item")
    match = re.fullmatch(r"p(\d+)-p(\d+)", work.range_key)
    if not match:
        raise ValueError("unsupported persisted range key")
    start, end = int(match.group(1)), int(match.group(2))
    scanned = {int(page) for page in batch.scanned_pages}
    if any(not start <= page <= end for page in scanned):
        raise ValueError("scanned page outside persisted range")
    if any(int(candidate.page_number) not in scanned for candidate in batch.candidates):
        raise ValueError("candidate page outside scanned page set")


def _candidate_values(
    candidate: Candidate,
    *,
    job_id: uuid.UUID,
    range_work_id: uuid.UUID | None,
    stable_key: str | None = None,
) -> dict[str, Any]:
    key = stable_key or candidate.stable_key
    if not key:
        raise ValueError("candidate.stable_key is required for durable persistence")
    return {
        "candidate_id": uuid.uuid5(job_id, key),
        "job_id": job_id,
        "stable_key": key,
        "block_ref": {"segment_ids": list(candidate.segment_ids), "page": candidate.page_number,
                      "original_source": candidate.original_source},
        "region_ref": {"bbox": candidate.bbox, "page": candidate.page_number} if candidate.bbox else None,
        "span_ref": {"segments": list(candidate.segment_ids)},
        "window": candidate.text,
        "context": candidate.context,
        "strength": candidate.strength,
        "source": candidate.source,
        "state": "pending",
        "range_work_id": range_work_id,
    }


class CandidateRepository:
    """Read/write candidate ledger without owning transaction boundaries."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def upsert(
        self,
        candidate: Candidate,
        *,
        job_id: uuid.UUID,
        range_work_id: uuid.UUID | None = None,
    ) -> ParseCandidate:
        values = _candidate_values(candidate, job_id=job_id, range_work_id=range_work_id)
        dialect = self.db.get_bind().dialect.name
        insert = sqlite_insert if dialect == "sqlite" else pg_insert
        statement = insert(ParseCandidate).values(**values)
        returned_statement = statement.on_conflict_do_update(
            index_elements=[ParseCandidate.job_id, ParseCandidate.stable_key],
            set_={key: getattr(statement.excluded, key) for key in (
                "block_ref", "region_ref", "span_ref", "window", "context", "strength", "source"
            )},
        ).returning(ParseCandidate).execution_options(populate_existing=True)
        # First discovery owns range_work_id; rescanning another strategy never steals its
        # provenance.  Candidate judgement and evidence likewise survive every upsert.
        return (await self.db.execute(returned_statement)).scalar_one()

    async def upsert_many(
        self,
        candidates: Iterable[Candidate],
        *,
        job_id: uuid.UUID,
        range_work_id: uuid.UUID | None = None,
    ) -> list[ParseCandidate]:
        result: list[ParseCandidate] = []
        for candidate in candidates:
            result.append(await self.upsert(candidate, job_id=job_id, range_work_id=range_work_id))
        return result

    async def pending(
        self,
        *,
        job_id: uuid.UUID,
        range_work_id: uuid.UUID | None = None,
        limit: int = 100,
        after_stable_key: str | None = None,
    ) -> list[ParseCandidate]:
        if limit < 1:
            raise ValueError("limit must be positive")
        query = select(ParseCandidate).where(
            ParseCandidate.job_id == job_id,
            ParseCandidate.state == "pending",
        ).order_by(ParseCandidate.stable_key).limit(limit)
        if range_work_id is not None:
            query = query.where(ParseCandidate.range_work_id == range_work_id)
        if after_stable_key is not None:
            query = query.where(ParseCandidate.stable_key > after_stable_key)
        return list((await self.db.execute(query)).scalars().all())

    async def initialize_ranges(self, job_id: uuid.UUID, shards: Sequence[Any]) -> list[ParseRangeWorkItem]:
        if not shards:
            return []
        dialect = self.db.get_bind().dialect.name
        insert = sqlite_insert if dialect == "sqlite" else pg_insert
        shard_keys = {(str(shard.strategy_version), str(shard.range_key)): shard for shard in shards}
        existing = list((await self.db.execute(select(ParseRangeWorkItem).where(
            ParseRangeWorkItem.job_id == job_id,
        ))).scalars().all())
        existing_keys = {(row.strategy_version, row.range_key) for row in existing}
        missing = [shard for key, shard in shard_keys.items() if key not in existing_keys]
        if missing:
            values = [{"work_id": uuid.uuid5(job_id, shard.shard_key), "job_id": job_id,
                       "strategy_version": shard.strategy_version, "range_key": shard.range_key,
                       "state": "pending", "scan_done": False, "quality_blockers": []}
                      for shard in missing]
            for offset in range(0, len(values), 1000):
                statement = insert(ParseRangeWorkItem).values(values[offset:offset + 1000]).on_conflict_do_nothing(
                    index_elements=["job_id", "strategy_version", "range_key"])
                await self.db.execute(statement)
            existing = list((await self.db.execute(select(ParseRangeWorkItem).where(
                ParseRangeWorkItem.job_id == job_id,
            ))).scalars().all())
        order = {(str(shard.strategy_version), str(shard.range_key)): index
                 for index, shard in enumerate(shards)}
        return sorted((row for row in existing if (row.strategy_version, row.range_key) in order),
                      key=lambda row: order[(row.strategy_version, row.range_key)])

    async def persist_scan(self, work_id: uuid.UUID, batch: Any) -> list[ParseCandidate]:
        """Upsert discoveries and advance only scan progress in the same outer transaction."""
        work = await self.db.get(ParseRangeWorkItem, work_id, with_for_update=True)
        if work is None:
            raise KeyError(f"range work item not found: {work_id}")
        job = await self.db.get(ParseFieldJob, work.job_id)
        if job is None:
            raise KeyError(f"field job not found: {work.job_id}")
        validate_scan_batch(work, batch, job.revision_id)
        rows = await self.upsert_many(batch.candidates, job_id=work.job_id, range_work_id=work_id)
        work.cursor = batch.cursor.as_dict()
        work.scan_done = batch.done
        work.quality_blockers = sorted({*(work.quality_blockers or []),
            *(f"unreadable_page:{p}" for p in batch.unreadable_pages)})
        # scan_done does not imply all candidates have been judged.
        work.state = "scanned" if batch.done else "scanning"
        await self.db.flush()
        return rows
    async def set_state(
        self,
        candidate_id: uuid.UUID,
        state: str,
        *,
        judgement: str | None = None,
        evidence: dict[str, Any] | None = None,
        split_path: Sequence[str] | None = None,
    ) -> None:
        if state not in ALL_STATES:
            raise ValueError(f"unknown candidate state: {state}")
        values: dict[str, Any] = {"state": state}
        if judgement is not None:
            values["judgement"] = judgement
        if evidence is not None:
            values["evidence"] = evidence
        if split_path is not None:
            values["split_path"] = list(split_path)
        await self.db.execute(update(ParseCandidate).where(
            ParseCandidate.candidate_id == candidate_id,
            ParseCandidate.state.in_(("pending", "in_flight", state)),
        ).values(**values))

    async def advance_scan_cursor(
        self,
        work_id: uuid.UUID,
        cursor: ScanCursor | dict[str, Any],
        *,
        scan_done: bool | None = None,
        quality_blockers: Sequence[str] | None = None,
    ) -> None:
        payload = cursor.as_dict() if isinstance(cursor, ScanCursor) else dict(cursor)
        values: dict[str, Any] = {"cursor": payload}
        if scan_done is not None:
            values["scan_done"] = bool(scan_done)
        if quality_blockers is not None:
            values["quality_blockers"] = list(quality_blockers)
        await self.db.execute(update(ParseRangeWorkItem).where(ParseRangeWorkItem.work_id == work_id).values(**values))

    async def get_scan_cursor(self, work_id: uuid.UUID) -> ScanCursor:
        row = await self.db.get(ParseRangeWorkItem, work_id)
        if row is None:
            raise KeyError(f"range work item not found: {work_id}")
        return ScanCursor.from_dict(row.cursor)

