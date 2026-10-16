"""附件登记、可恢复导入与不可变业务结果。

一个 Attachment 对应一个业务范围和一个 Document；原文件与内容修订分开。
下载状态同时作为持久化工作队列，消息只负责唤醒，数据库是恢复依据。
"""
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, ForeignKey, Index, Integer, LargeBinary, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.database import Base


class Attachment(Base):
    __tablename__ = "attachments"
    __table_args__ = (UniqueConstraint("source_system", "source_id", name="uq_attachment_source"),
                      Index("ix_attachment_scope", "mine_code", "file_name_id", "period_type", "period_key"))
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_system: Mapped[str] = mapped_column(String(100))
    source_id: Mapped[str] = mapped_column(String(200))
    mine_code: Mapped[str] = mapped_column(String(100), index=True)
    mine_name: Mapped[str] = mapped_column(String(200))
    file_name_id: Mapped[str] = mapped_column(String(200), index=True)
    file_name: Mapped[str] = mapped_column(String(500))
    report_time_raw: Mapped[str] = mapped_column(String(100))
    update_rate: Mapped[int] = mapped_column(Integer)
    cycle: Mapped[str] = mapped_column(String(20), default="")
    period_type: Mapped[str] = mapped_column(String(30), index=True)
    period_key: Mapped[str] = mapped_column(String(100), index=True)
    normalization_status: Mapped[str] = mapped_column(String(40), default="NORMALIZED")
    attachment_name: Mapped[str] = mapped_column(String(500), index=True)
    source_url: Mapped[str] = mapped_column(Text)  # 来源链接；开发期导入按原值保存。
    format: Mapped[str] = mapped_column(String(20), index=True)
    checklist_type: Mapped[Optional[str]] = mapped_column(String(100), index=True)
    document_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("documents.document_id"), unique=True)
    current_revision_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    current_version_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), index=True)
    cas_version: Mapped[int] = mapped_column(Integer, default=0)
    version_seq: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(40), default="REGISTERED", index=True)
    download_state: Mapped[str] = mapped_column(String(40), default="QUEUED", index=True)
    download_attempts: Mapped[int] = mapped_column(Integer, default=0)
    download_fence: Mapped[int] = mapped_column(Integer, default=0)
    lease_owner: Mapped[Optional[str]] = mapped_column(String(100))
    lease_until: Mapped[Optional[datetime]] = mapped_column(index=True)
    next_attempt_at: Mapped[Optional[datetime]] = mapped_column(index=True)
    last_error: Mapped[Optional[str]] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(default=datetime.utcnow, onupdate=datetime.utcnow)


class AttachmentFileRevision(Base):
    __tablename__ = "attachment_file_revisions"
    __table_args__ = (UniqueConstraint("attachment_id", "sha256", name="uq_attachment_revision_hash"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    attachment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("attachments.id"), index=True)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    size: Mapped[int] = mapped_column(BigInteger)
    mime_type: Mapped[str] = mapped_column(String(100))
    object_key: Mapped[str] = mapped_column(Text)
    source_digest: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(40))  # ACCEPTED / ID_CONTENT_CONFLICT
    metadata_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)


class AttachmentVersionSummary(Base):
    """列表和统计用的小摘要。完整 snapshot 仍只在版本详情里读取。"""

    __tablename__ = "attachment_version_summaries"
    version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("attachment_versions.id"), primary_key=True)
    summary: Mapped[dict] = mapped_column(JSON, nullable=False)


class AttachmentVersion(Base):
    __tablename__ = "attachment_versions"
    __table_args__ = (UniqueConstraint("attachment_id", "number", name="uq_attachment_version_number"),
                      UniqueConstraint("attachment_id", "request_id", name="uq_attachment_version_request"))
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    attachment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("attachments.id"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    request_id: Mapped[str] = mapped_column(String(200))
    operation: Mapped[str] = mapped_column(String(40))
    parent_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("attachment_versions.id"))
    expected_cas: Mapped[int] = mapped_column(Integer)
    file_revision_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("attachment_file_revisions.id"))
    run_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("parse_runs.parse_run_id"), unique=True)
    build_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("content_build_jobs.build_id"))
    state: Mapped[str] = mapped_column(String(40), default="QUEUED", index=True)
    actor: Mapped[str] = mapped_column(String(200))
    reason: Mapped[str] = mapped_column(String(2000))
    inherited_fields: Mapped[list] = mapped_column(JSON, default=list)
    snapshot: Mapped[Optional[dict]] = mapped_column(JSON(none_as_null=True))  # 定版后禁止覆盖/删除。
    last_error: Mapped[Optional[str]] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow, index=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column()


class AttachmentImport(Base):
    __tablename__ = "attachment_imports"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_system: Mapped[str] = mapped_column(String(100))
    file_name: Mapped[str] = mapped_column(String(500))
    file_hash: Mapped[str] = mapped_column(String(64))
    staging_file: Mapped[Optional[bytes]] = mapped_column(LargeBinary)
    state: Mapped[str] = mapped_column(String(40), default="PREVIEW_QUEUED", index=True)
    mappings: Mapped[dict] = mapped_column(JSON, default=dict)
    counts: Mapped[dict] = mapped_column(JSON, default=dict)
    last_error: Mapped[Optional[str]] = mapped_column(String(500))
    actor: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow, index=True)
    confirmed_at: Mapped[Optional[datetime]] = mapped_column()
    control_action: Mapped[Optional[str]] = mapped_column(String(20))
    control_after: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    control_group: Mapped[Optional[str]] = mapped_column(String(200))
    control_actor: Mapped[Optional[str]] = mapped_column(String(200))
    control_reason: Mapped[Optional[str]] = mapped_column(String(2000))


class AttachmentImportRow(Base):
    __tablename__ = "attachment_import_rows"
    __table_args__ = (UniqueConstraint("import_id", "sheet", "row_number", name="uq_attachment_import_row"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    import_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("attachment_imports.id"), index=True)
    sheet: Mapped[str] = mapped_column(String(200))
    row_number: Mapped[int] = mapped_column(Integer)
    raw: Mapped[dict] = mapped_column(JSON)
    normalized: Mapped[dict] = mapped_column(JSON)
    source_url: Mapped[Optional[str]] = mapped_column(Text)  # 导入行中的来源链接。
    state: Mapped[str] = mapped_column(String(40), index=True)
    errors: Mapped[list] = mapped_column(JSON, default=list)
    attachment_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("attachments.id"), index=True)
    observation: Mapped[Optional[dict]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)


class AttachmentChecklistMapping(Base):
    __tablename__ = "attachment_checklist_mappings"
    __table_args__ = (UniqueConstraint("source_system", "file_name_id", name="uq_attachment_checklist_mapping"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_system: Mapped[str] = mapped_column(String(100))
    file_name_id: Mapped[str] = mapped_column(String(200))
    checklist_type: Mapped[str] = mapped_column(String(100))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    actor: Mapped[str] = mapped_column(String(200))
    updated_at: Mapped[datetime] = mapped_column(default=datetime.utcnow, onupdate=datetime.utcnow)


APPEND_ONLY_DDL = ["""
CREATE OR REPLACE FUNCTION protect_attachment_history() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_TABLE_NAME = 'attachment_versions' THEN
    IF OLD.snapshot IS NULL AND TG_OP = 'UPDATE' THEN RETURN NEW; END IF;
  END IF;
  RAISE EXCEPTION 'attachment history is immutable';
END; $$
"""]
for _table in ("attachment_versions", "attachment_file_revisions"):
    APPEND_ONLY_DDL.extend([
        f"DROP TRIGGER IF EXISTS protect_history ON {_table}",
        f"CREATE TRIGGER protect_history BEFORE UPDATE OR DELETE ON {_table} FOR EACH ROW EXECUTE FUNCTION protect_attachment_history()",
    ])
