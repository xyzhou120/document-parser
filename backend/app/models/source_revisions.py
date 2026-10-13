"""针对不可变 PDF 产物的只追加来源复核决策。"""

import uuid
from datetime import datetime

from sqlalchemy import Integer, String, Text, event
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.database import Base


class SourceRevision(Base):
    __tablename__ = "source_revisions"

    revision_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    parent_artifact_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    derived_artifact_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    parent_revision_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    revision_type: Mapped[str] = mapped_column(String(20))  # CORRECT / CONFIRM / UNREADABLE
    page_number: Mapped[int] = mapped_column(Integer)
    bbox: Mapped[dict | None] = mapped_column(JSON)
    start_offset: Mapped[int | None] = mapped_column(Integer)
    end_offset: Mapped[int | None] = mapped_column(Integer)
    original_text: Mapped[str | None] = mapped_column(Text)
    original_text_hash: Mapped[str | None] = mapped_column(String(64))
    replacement_text: Mapped[str | None] = mapped_column(Text)
    comment: Mapped[str] = mapped_column(Text, default="")
    affected_fields: Mapped[list] = mapped_column(JSON, default=list)
    source_review_status: Mapped[str] = mapped_column(String(30), default="PARTIALLY_REVIEWED")
    extraction_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    extraction_task_id: Mapped[str | None] = mapped_column(String(255))
    created_by: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow, index=True)


def _reject_mutation(mapper, connection, target):
    raise ValueError("source_revisions is append-only")


event.listen(SourceRevision, "before_update", _reject_mutation)
event.listen(SourceRevision, "before_delete", _reject_mutation)
