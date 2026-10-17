"""共享执行身份与附件订阅者。

共享结果只负责复用昂贵的模型调用；业务附件仍然拥有自己的
AttachmentVersion、ParseRun、内容修订、head 和人工复核边界。
"""
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.database import Base


class SharedExecution(Base):
    __tablename__ = "shared_executions"
    __table_args__ = (
        UniqueConstraint("identity_hash", name="uq_shared_execution_identity"),
    )

    shared_execution_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    identity_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    file_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    content_digest: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    coverage_digest: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    preprocess_version: Mapped[str] = mapped_column(String(120), nullable=False)
    contract_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    model_signature: Mapped[str] = mapped_column(String(64), nullable=False)
    source_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("parse_runs.parse_run_id"), nullable=True, index=True)
    source_version_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("attachment_versions.id"), nullable=True, index=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="QUEUED", index=True)
    execution_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fence_token: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    manifest: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    failure_code: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    lease_owner: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    lease_until: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow, index=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)


class SharedExecutionSubscriber(Base):
    __tablename__ = "shared_execution_subscribers"
    __table_args__ = (
        UniqueConstraint("shared_execution_id", "parse_run_id", name="uq_shared_subscriber_run"),
        UniqueConstraint("target_version_id", name="uq_shared_subscriber_version"),
    )

    subscriber_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    shared_execution_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("shared_executions.shared_execution_id"), nullable=False, index=True)
    target_attachment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("attachments.id"), nullable=False, index=True)
    target_document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.document_id"), nullable=False, index=True)
    target_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("attachment_versions.id"), nullable=False)
    parse_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("parse_runs.parse_run_id"), nullable=False, index=True)
    expected_cas: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_invalidation_watermark: Mapped[Optional[int]] = mapped_column(nullable=True)
    requested_contract_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="WAITING", index=True)
    source_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failure_code: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    provenance: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow, index=True)
    attached_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
