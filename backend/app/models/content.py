"""不可变内容修订、索引版本与失效记录。"""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Integer, String, Text, UniqueConstraint, event
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.database import Base


def utcnow() -> datetime:
    return datetime.utcnow()


class ContentBuildJob(Base):
    __tablename__ = "content_build_jobs"
    __table_args__ = (CheckConstraint("attempt >= 0 AND fence_token >= 0", name="ck_content_build_counters"),)

    build_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("documents.document_id"), index=True)
    file_hash: Mapped[str] = mapped_column(String(64), index=True)
    profile: Mapped[dict] = mapped_column(JSON, default=dict)
    parent_revision_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("content_revisions.revision_id"), nullable=True, index=True)
    phase: Mapped[str] = mapped_column(String(50), default="planned", index=True)
    checkpoint: Mapped[dict] = mapped_column(JSON, default=dict)
    dispatch_state: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    attempt: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    lease_owner: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    lease_until: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    fence_token: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class ContentRevision(Base):
    __tablename__ = "content_revisions"
    __table_args__ = (UniqueConstraint("origin_source_revision_id", name="uq_content_revision_source_origin"),)

    revision_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("documents.document_id"), index=True)
    artifact_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("document_artifacts.artifact_id"), index=True)
    parent_revision_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("content_revisions.revision_id"), nullable=True, index=True)
    origin_source_revision_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("source_revisions.revision_id"), nullable=True, index=True)
    origin_event_id: Mapped[Optional[str]] = mapped_column(String(200), nullable=True, index=True)
    file_hash: Mapped[str] = mapped_column(String(64), index=True)
    content_digest: Mapped[str] = mapped_column(String(64), index=True)
    preprocess_manifest: Mapped[dict] = mapped_column(JSON, default=dict)
    adapter_version: Mapped[str] = mapped_column(String(80))
    source_kind: Mapped[str] = mapped_column(String(50))
    change_kind: Mapped[str] = mapped_column(String(50))
    coverage_manifest: Mapped[dict] = mapped_column(JSON, default=dict)
    coverage_digest: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)


class ContentImpact(Base):
    __tablename__ = "content_impacts"

    impact_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    from_revision_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("content_revisions.revision_id"), index=True)
    to_revision_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("content_revisions.revision_id"), index=True)
    changed_ranges: Mapped[list] = mapped_column(JSON, default=list)
    dependency_precision: Mapped[str] = mapped_column(String(32), default="unknown")
    recompute_policy: Mapped[str] = mapped_column(String(32), default="full")
    invalidation_scope: Mapped[dict] = mapped_column(JSON, default=dict)
    request_id: Mapped[Optional[str]] = mapped_column(String(200), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)


class ContentInvalidation(Base):
    __tablename__ = "content_invalidations"

    invalidation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("documents.document_id"), index=True)
    invalidation_kind: Mapped[str] = mapped_column(String(40), index=True)
    scope_selector: Mapped[dict] = mapped_column(JSON, default=dict)
    effective_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)


class DocumentProcessingHead(Base):
    __tablename__ = "document_processing_heads"
    __table_args__ = (CheckConstraint("cas_version >= 0 AND invalidation_watermark >= 0", name="ck_processing_head_counters"),)

    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("documents.document_id"), primary_key=True)
    active_content_revision_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("content_revisions.revision_id"), nullable=True, index=True)
    current_parse_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("parse_runs.parse_run_id"), nullable=True, index=True)
    active_index_generation_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("index_generations.generation_id"), nullable=True, index=True)
    cas_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    invalidation_watermark: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class IndexGeneration(Base):
    __tablename__ = "index_generations"
    __table_args__ = (
        UniqueConstraint("revision_id", "config_hash", "snapshot_hash", name="uq_index_generation_revision_config_snapshot"),
        CheckConstraint("serve_status IN ('unavailable','eligible','blocked')", name="ck_index_generation_serve_status"),
        CheckConstraint("build_status IN ('queued','building','succeeded','failed')", name="ck_index_generation_build_status"),
        CheckConstraint("coverage_status IN ('unknown','partial','full')", name="ck_index_generation_coverage_status"),
        CheckConstraint("serve_version >= 0", name="ck_index_generation_serve_version"),
    )

    generation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    revision_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("content_revisions.revision_id"), index=True)
    config_hash: Mapped[str] = mapped_column(String(64))
    snapshot_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    build_status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    coverage_status: Mapped[str] = mapped_column(String(32), default="unknown", index=True)
    serve_status: Mapped[str] = mapped_column(String(32), default="unavailable", index=True)
    serve_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    indexed_block_manifest: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class IndexChunk(Base):
    __tablename__ = "index_chunks"
    __table_args__ = (UniqueConstraint("generation_id", "chunk_id", name="uq_index_chunk_generation_key"),)

    chunk_row_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    generation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("index_generations.generation_id"), index=True)
    chunk_id: Mapped[str] = mapped_column(String(300))
    block_ref: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    table_ref: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    text: Mapped[str] = mapped_column(Text)
    embedding: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    embedding_config: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)


def _reject_mutation(mapper, connection, target):
    raise ValueError(f"{target.__tablename__} is append-only")


for _immutable_model in (ContentRevision, ContentImpact, ContentInvalidation):
    event.listen(_immutable_model, "before_update", _reject_mutation)
    event.listen(_immutable_model, "before_delete", _reject_mutation)
