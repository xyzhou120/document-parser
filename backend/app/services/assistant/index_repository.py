"""Persistence boundary for immutable index generations.

The memory repository is intentionally deterministic for tests and local probes;
the SQL repository uses the same methods and short transactions in workers.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.models.content import (
    ContentInvalidation,
    ContentRevision,
    DocumentProcessingHead,
    IndexChunk,
    IndexGeneration,
)


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), default=str).encode()).hexdigest()


@dataclass
class GenerationSnapshot:
    generation_id: UUID
    revision_id: UUID
    document_id: UUID
    config_hash: str
    snapshot_hash: str
    build_status: str
    coverage_status: str
    serve_status: str
    serve_version: int
    indexed_block_manifest: dict
    chunks: list[dict] = field(default_factory=list)
    invalidation_watermark: int = 0
    created_at: datetime | None = None


@dataclass
class HeadSnapshot:
    document_id: UUID
    active_content_revision_id: UUID | None = None
    active_index_generation_id: UUID | None = None
    cas_version: int = 0
    invalidation_watermark: int = 0


class IndexRepository:
    """SQLAlchemy implementation; callers own model/content validation."""

    def __init__(self, sessions):
        self.sessions = sessions

    async def find_generation(self, revision_id, config_hash, snapshot_hash):
        async with self.sessions() as db:
            row = (await db.execute(select(IndexGeneration).where(
                IndexGeneration.revision_id == UUID(str(revision_id)),
                IndexGeneration.config_hash == config_hash,
                IndexGeneration.snapshot_hash == snapshot_hash))).scalar_one_or_none()
            if row is None:
                return None
            chunks = (await db.execute(select(IndexChunk).where(
                IndexChunk.generation_id == row.generation_id).order_by(IndexChunk.chunk_id))).scalars().all()
            return _generation_snapshot(row, chunks)

    async def create_generation(self, *, revision_id, document_id, config_hash, snapshot_hash,
                                manifest, chunks, coverage_status, invalidation_watermark=0):
        async with self.sessions() as db, db.begin():
            gid = uuid4()
            now = datetime.utcnow()
            full_manifest = {**deepcopy(manifest), "document_id": str(document_id),
                             "invalidation_watermark": invalidation_watermark}
            inserted = (await db.execute(insert(IndexGeneration).values(
                generation_id=gid, revision_id=UUID(str(revision_id)), config_hash=config_hash,
                snapshot_hash=snapshot_hash, build_status="building", coverage_status=coverage_status,
                serve_status="unavailable", serve_version=0, indexed_block_manifest=full_manifest,
                created_at=now, updated_at=now).on_conflict_do_nothing(
                    index_elements=["revision_id", "config_hash", "snapshot_hash"])
                .returning(IndexGeneration.generation_id))).scalar_one_or_none()
            generation = (await db.execute(select(IndexGeneration).where(
                IndexGeneration.revision_id == UUID(str(revision_id)),
                IndexGeneration.config_hash == config_hash,
                IndexGeneration.snapshot_hash == snapshot_hash))).scalar_one()
            if inserted:
                for item in chunks:
                    db.add(IndexChunk(generation_id=generation.generation_id,
                                      chunk_id=item["chunk_id"], block_ref=item.get("block_ref"),
                                      table_ref=item.get("table_ref"), text=item["text"],
                                      embedding=item.get("embedding"),
                                      embedding_config=item.get("embedding_config")))
                generation.build_status = "succeeded"
                await db.flush()
            rows = (await db.execute(select(IndexChunk).where(
                IndexChunk.generation_id == generation.generation_id).order_by(IndexChunk.chunk_id))).scalars().all()
            return _generation_snapshot(generation, rows)

    async def get_generation(self, generation_id):
        async with self.sessions() as db:
            row = await db.get(IndexGeneration, UUID(str(generation_id)))
            if row is None:
                return None
            chunks = (await db.execute(select(IndexChunk).where(
                IndexChunk.generation_id == row.generation_id).order_by(IndexChunk.chunk_id))).scalars().all()
            return _generation_snapshot(row, chunks)

    async def get_head(self, document_id, *, lock=False):
        async with self.sessions() as db:
            query = select(DocumentProcessingHead).where(
                DocumentProcessingHead.document_id == UUID(str(document_id)))
            if lock:
                query = query.with_for_update()
            row = (await db.execute(query)).scalar_one_or_none()
            return _head_snapshot(row) if row else None

    async def publish_generation(self, generation_id, *, expected_cas_version,
                                 expected_revision_id, expected_invalidation_watermark):
        async with self.sessions() as db, db.begin():
            generation = await db.get(IndexGeneration, UUID(str(generation_id)))
            if generation is None:
                raise ValueError("generation does not exist")
            revision = await db.get(ContentRevision, generation.revision_id)
            if revision is None or revision.revision_id != UUID(str(expected_revision_id)):
                raise ValueError("generation revision mismatch")
            head = await db.get(DocumentProcessingHead, revision.document_id, with_for_update=True)
            if head is None or head.cas_version != expected_cas_version:
                raise ValueError("processing head CAS conflict")
            if head.active_content_revision_id != revision.revision_id:
                raise ValueError("revision is not active")
            if head.invalidation_watermark != expected_invalidation_watermark:
                raise ValueError("invalidation watermark changed")
            generation = (await db.execute(select(IndexGeneration).where(
                IndexGeneration.generation_id == generation.generation_id).with_for_update()
                .execution_options(populate_existing=True))).scalar_one()
            if generation.indexed_block_manifest.get("publication_cas_version") != expected_cas_version:
                raise ValueError("generation publication snapshot CAS conflict")
            if (generation.build_status != "succeeded" or generation.coverage_status not in {"partial", "full"}
                    or generation.serve_status == "blocked"):
                raise ValueError("only a safe succeeded generation can be served")
            invalidations = (await db.execute(select(ContentInvalidation).where(
                ContentInvalidation.document_id == revision.document_id,
                ContentInvalidation.effective_at <= datetime.utcnow()))).scalars().all()
            if any(invalidation_applies(row, generation) for row in invalidations):
                raise ValueError("generation source invalidated")
            generation.serve_status = "eligible"
            generation.serve_version += 1
            head.active_index_generation_id = generation.generation_id
            head.cas_version += 1
            return _generation_snapshot(generation, [])

    async def invalidate_generation(self, generation_id, reason="content_invalidated"):
        async with self.sessions() as db, db.begin():
            row = await db.get(IndexGeneration, UUID(str(generation_id)), with_for_update=True)
            if row is None:
                return False
            row.serve_status = "blocked"
            row.serve_version += 1
            return True

    async def is_generation_current(self, generation_id, *, revision_id, document_id,
                                    invalidation_watermark):
        async with self.sessions() as db:
            generation = await db.get(IndexGeneration, UUID(str(generation_id)))
            head = await db.get(DocumentProcessingHead, UUID(str(document_id)))
            if generation is None or head is None:
                return False
            if (generation.serve_status != "eligible" or generation.revision_id != UUID(str(revision_id)) or
                    head.active_content_revision_id != generation.revision_id or
                    head.active_index_generation_id != generation.generation_id or
                    head.invalidation_watermark != invalidation_watermark):
                return False
            invalidated = (await db.execute(select(ContentInvalidation).where(
                ContentInvalidation.document_id == UUID(str(document_id)),
                ContentInvalidation.effective_at <= datetime.utcnow()))).scalars().all()
            return not any(invalidation_applies(row, generation) for row in invalidated)

    async def lock_answer_qualification(self, generation_id, *, expected_revision_id,
                                        expected_serve_version, expected_invalidation_watermark,
                                        document_id, db):
        """Caller owns this transaction and writes the answer before releasing locks."""
        head = await db.get(DocumentProcessingHead, UUID(str(document_id)), with_for_update=True,
                            populate_existing=True)
        generation = await db.get(IndexGeneration, UUID(str(generation_id)), with_for_update=True,
                                  populate_existing=True)
        if not head or not generation:
            return {"eligible": False, "reason": "generation_unavailable"}
        revision = await db.get(ContentRevision, generation.revision_id)
        invalidations = (await db.execute(select(ContentInvalidation).where(
            ContentInvalidation.document_id == UUID(str(document_id)),
            ContentInvalidation.effective_at <= datetime.utcnow()))).scalars().all()
        eligible = (revision is not None and revision.document_id == UUID(str(document_id)) and
                    generation.serve_status == "eligible" and generation.build_status == "succeeded" and
                    generation.coverage_status in {"partial", "full"} and
                    generation.serve_version == expected_serve_version and
                    generation.revision_id == UUID(str(expected_revision_id)) and
                    head.active_content_revision_id == generation.revision_id and
                    head.active_index_generation_id == generation.generation_id and
                    head.invalidation_watermark == expected_invalidation_watermark and
                    not any(invalidation_applies(row, generation) for row in invalidations))
        return {"eligible": eligible,
                "reason": None if eligible else "generation_changed",
                "generation_id": str(generation.generation_id),
                "revision_id": str(generation.revision_id),
                "serve_version": generation.serve_version,
                "invalidation_watermark": head.invalidation_watermark}


def _generation_snapshot(row, chunks):
    return GenerationSnapshot(
        generation_id=row.generation_id, revision_id=row.revision_id,
        document_id=getattr(row, "document_id", None) or UUID(str(row.indexed_block_manifest.get("document_id"))),
        config_hash=row.config_hash, snapshot_hash=row.snapshot_hash,
        build_status=row.build_status, coverage_status=row.coverage_status,
        serve_status=row.serve_status, serve_version=row.serve_version,
        indexed_block_manifest=deepcopy(row.indexed_block_manifest or {}),
        chunks=[_chunk_snapshot(c) for c in chunks], created_at=row.created_at,
        invalidation_watermark=int((row.indexed_block_manifest or {}).get("invalidation_watermark", 0)),
    )


def invalidation_applies(invalidation, generation):
    """Conservative selector adapter; scoped field/fact changes do not revoke raw text."""
    selector = invalidation.scope_selector or {}
    scope = selector.get("scope") or selector.get("scope_type") or selector.get("kind") or invalidation.invalidation_kind
    if scope in {"field", "fact", "parse_run"}:
        return False
    if any(key in selector for key in ("parse_run_id", "field_key", "fact_id")) and not any(
            key in selector for key in ("revision_id", "content_revision_id", "generation_id",
                                        "index_generation_id", "pages", "block_ids", "evidence_ids", "evidence_id")):
        return False
    revision = selector.get("revision_id") or selector.get("content_revision_id")
    generation_id = selector.get("generation_id") or selector.get("index_generation_id")
    if revision is not None and str(revision) != str(generation.revision_id):
        return False
    if generation_id is not None and str(generation_id) != str(generation.generation_id):
        return False
    pages = {int(page) for page in selector.get("pages", []) or []}
    blocks = {str(block) for block in selector.get("block_ids", []) or []}
    if pages or blocks:
        manifest = generation.indexed_block_manifest or {}
        if not manifest.get("block_manifest"):
            return True
        manifest_pages = {int(item.get("page_number")) for item in manifest.get("block_manifest", [])
                          if item.get("page_number") is not None}
        manifest_blocks = {str(item.get("block_id")) for item in manifest.get("block_manifest", [])
                           if item.get("block_id") is not None}
        if pages and not (pages & manifest_pages):
            return False
        if blocks and not (blocks & manifest_blocks):
            return False
    # Evidence/block/region scope conservatively blocks the entire generation.
    return True


def _chunk_snapshot(row):
    return {"chunk_id": row.chunk_id, "block_ref": deepcopy(row.block_ref),
            "table_ref": deepcopy(row.table_ref), "text": row.text,
            "embedding": deepcopy(row.embedding), "embedding_config": deepcopy(row.embedding_config)}


def _head_snapshot(row):
    return HeadSnapshot(row.document_id, row.active_content_revision_id,
                        row.active_index_generation_id, row.cas_version,
                        row.invalidation_watermark)


class MemoryIndexRepository:
    """Deterministic repository for tests and development; mirrors publication guards."""

    def __init__(self):
        self.generations: dict[UUID, GenerationSnapshot] = {}
        self.by_identity: dict[tuple, UUID] = {}
        self.heads: dict[UUID, HeadSnapshot] = {}
        self.invalidated: set[UUID] = set()

    async def find_generation(self, revision_id, config_hash, snapshot_hash):
        gid = self.by_identity.get((UUID(str(revision_id)), config_hash, snapshot_hash))
        return deepcopy(self.generations[gid]) if gid else None

    async def create_generation(self, *, revision_id, document_id, config_hash, snapshot_hash,
                                manifest, chunks, coverage_status, invalidation_watermark=0):
        key = (UUID(str(revision_id)), config_hash, snapshot_hash)
        if key in self.by_identity:
            return deepcopy(self.generations[self.by_identity[key]])
        gid = uuid4()
        snapshot = GenerationSnapshot(gid, UUID(str(revision_id)), UUID(str(document_id)), config_hash,
                                      snapshot_hash, "succeeded", coverage_status, "unavailable", 0,
                                      {**deepcopy(manifest), "document_id": str(document_id)},
                                      deepcopy(chunks), invalidation_watermark, datetime.utcnow())
        self.by_identity[key], self.generations[gid] = gid, snapshot
        return deepcopy(snapshot)

    async def get_generation(self, generation_id):
        row = self.generations.get(UUID(str(generation_id)))
        return deepcopy(row) if row else None

    async def get_head(self, document_id, *, lock=False):
        row = self.heads.get(UUID(str(document_id)))
        return deepcopy(row) if row else None

    def ensure_head(self, document_id, revision_id, *, cas_version=0, invalidation_watermark=0):
        self.heads[UUID(str(document_id))] = HeadSnapshot(UUID(str(document_id)), UUID(str(revision_id)),
                                                           None, cas_version, invalidation_watermark)

    async def publish_generation(self, generation_id, *, expected_cas_version,
                                 expected_revision_id, expected_invalidation_watermark):
        row = self.generations[UUID(str(generation_id))]
        head = self.heads[row.document_id]
        if (row.revision_id != UUID(str(expected_revision_id)) or head.cas_version != expected_cas_version or
                head.active_content_revision_id != row.revision_id or
                head.invalidation_watermark != expected_invalidation_watermark or
                row.build_status != "succeeded" or row.coverage_status not in {"partial", "full"} or
                row.serve_status == "blocked" or row.generation_id in self.invalidated):
            raise ValueError("generation publication guard failed")
        if row.indexed_block_manifest.get("publication_cas_version") != expected_cas_version:
            raise ValueError("generation publication snapshot CAS conflict")
        row.serve_status, row.serve_version = "eligible", row.serve_version + 1
        head.active_index_generation_id, head.cas_version = row.generation_id, head.cas_version + 1
        return deepcopy(row)

    async def invalidate_generation(self, generation_id, reason="content_invalidated"):
        row = self.generations.get(UUID(str(generation_id)))
        if not row:
            return False
        row.serve_status = "blocked"
        row.serve_version += 1
        self.invalidated.add(row.generation_id)
        return True

    async def is_generation_current(self, generation_id, *, revision_id, document_id,
                                    invalidation_watermark):
        row = self.generations.get(UUID(str(generation_id)))
        head = self.heads.get(UUID(str(document_id)))
        return bool(row and head and row.generation_id not in self.invalidated and
                    row.serve_status == "eligible" and row.revision_id == UUID(str(revision_id)) and
                    head.active_content_revision_id == row.revision_id and
                    head.active_index_generation_id == row.generation_id and
                    head.invalidation_watermark == invalidation_watermark)

    async def lock_answer_qualification(self, generation_id, *, expected_revision_id,
                                        expected_serve_version, expected_invalidation_watermark,
                                        document_id, db=None):
        generation = self.generations.get(UUID(str(generation_id)))
        head = self.heads.get(UUID(str(document_id)))
        eligible = bool(generation and head and generation.serve_status == "eligible" and
                        generation.serve_version == expected_serve_version and
                        generation.revision_id == UUID(str(expected_revision_id)) and
                        head.active_content_revision_id == generation.revision_id and
                        head.active_index_generation_id == generation.generation_id and
                        head.invalidation_watermark == expected_invalidation_watermark and
                        generation.generation_id not in self.invalidated)
        return {"eligible": eligible, "reason": None if eligible else "generation_changed",
                "generation_id": str(generation.generation_id) if generation else None,
                "revision_id": str(generation.revision_id) if generation else None,
                "serve_version": generation.serve_version if generation else None,
                "invalidation_watermark": head.invalidation_watermark if head else None}
