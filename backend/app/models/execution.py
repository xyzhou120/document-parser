"""Durable execution ledger for reliable extraction v2.

The tables in this module are the recovery truth for the new scheduler.  They
intentionally keep checkpoints and attempts append-friendly; workers use the
fence and execution generation columns to reject stale commits.
"""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.database import Base


def utcnow() -> datetime:
    return datetime.utcnow()


class ParseFieldJob(Base):
    __tablename__ = "parse_field_jobs"
    __table_args__ = (
        UniqueConstraint("run_id", "field_key", name="uq_parse_field_job_run_field"),
        CheckConstraint("execution_state IN ('queued','running','yielded','waiting_service','waiting_external_quota','paused','cancelled','done','failed_terminal','superseded')", name="ck_field_job_execution_state"),
        CheckConstraint("semantic_state IN ('unknown','completed','review_required','partial_candidates','not_found','incomplete')", name="ck_field_job_semantic_state"),
        CheckConstraint("fence_token >= 0 AND progress_seq >= 0 AND attempt >= 0", name="ck_field_job_counters"),
    )

    job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("parse_runs.parse_run_id"), index=True)
    field_key: Mapped[str] = mapped_column(String(200), index=True)
    rule_hash: Mapped[str] = mapped_column(String(64), index=True)
    revision_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("content_revisions.revision_id"), index=True)
    execution_state: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    semantic_state: Mapped[str] = mapped_column(String(32), default="unknown", index=True)
    phase: Mapped[str] = mapped_column(String(50), default="planned")
    checkpoint: Mapped[dict] = mapped_column(JSON, default=dict)
    next_attempt_at: Mapped[Optional[datetime]] = mapped_column(nullable=True, index=True)
    lease_owner: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    lease_until: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    fence_token: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    progress_seq: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow, index=True)


class ParseRangeWorkItem(Base):
    __tablename__ = "parse_range_work_items"
    __table_args__ = (UniqueConstraint("job_id", "strategy_version", "range_key", name="uq_parse_range_strategy"),)

    work_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("parse_field_jobs.job_id"), index=True)
    strategy_version: Mapped[str] = mapped_column(String(80), index=True)
    range_key: Mapped[str] = mapped_column(String(300), index=True)
    cursor: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    scan_done: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    state: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    quality_blockers: Mapped[list] = mapped_column(JSON, default=list)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class ParseCandidate(Base):
    __tablename__ = "parse_candidates"
    __table_args__ = (UniqueConstraint("job_id", "stable_key", name="uq_parse_candidate_stable_key"),)

    candidate_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("parse_field_jobs.job_id"), index=True)
    stable_key: Mapped[str] = mapped_column(String(500))
    block_ref: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    region_ref: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    span_ref: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    window: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    context: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    strength: Mapped[str] = mapped_column(String(32), default="weak", index=True)
    source: Mapped[str] = mapped_column(String(32), default="text", index=True)
    state: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    judgement: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    evidence: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    split_path: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    range_work_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("parse_range_work_items.work_id"), index=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class ParseModelCall(Base):
    __tablename__ = "parse_model_calls"
    __table_args__ = (
        UniqueConstraint("dispatch_key", name="uq_parse_model_calls_dispatch_key"),
        CheckConstraint("state IN ('queued','sending','started','succeeded','length','invalid','timeout','uncertain')", name="ck_model_call_state"),
        CheckConstraint("charge_state IN ('reserved','actual','uncertain','reconciled')", name="ck_model_call_charge_state"),
        CheckConstraint("fence >= 0 AND retry_seq >= 0 AND usage_revision >= 0", name="ck_model_call_counters"),
    )

    call_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("parse_field_jobs.job_id"), index=True)
    fence: Mapped[int] = mapped_column(Integer, nullable=False)
    provider_key: Mapped[str] = mapped_column(String(200), index=True)
    dispatch_key: Mapped[str] = mapped_column(String(500), index=True)
    checkpoint_key: Mapped[str] = mapped_column(String(300), index=True)
    retry_seq: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    retry_of_call_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("parse_model_calls.call_id"), nullable=True, index=True)
    candidate_set_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    split_path: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    state: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    charge_state: Mapped[str] = mapped_column(String(32), default="reserved", index=True)
    reservation: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    usage: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    usage_revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    unknown_deadline: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    usage_quality: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    finish_reason: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    provider_request_id: Mapped[Optional[str]] = mapped_column(String(300), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    sent_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)


class ExecutionOutbox(Base):
    __tablename__ = "execution_outbox"
    __table_args__ = (UniqueConstraint("kind", "dedupe_key", name="uq_execution_outbox_kind_dedupe"),)

    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(String(100), index=True)
    aggregate_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    dedupe_key: Mapped[str] = mapped_column(String(500))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    dispatch_state: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    next_attempt_at: Mapped[Optional[datetime]] = mapped_column(nullable=True, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
