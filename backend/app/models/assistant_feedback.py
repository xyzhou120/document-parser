"""Append-only samples for offline assistant/extraction quality improvement."""
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, ForeignKey, String, UniqueConstraint, event
from sqlalchemy.dialects.postgresql import JSON
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.database import Base


class AssistantFeedbackSample(Base):
    __tablename__ = "assistant_feedback_samples"
    __table_args__ = (
        UniqueConstraint("document_id", "request_id", name="uq_assistant_feedback_document_request"),
        CheckConstraint("sample_type IN ('qa','extraction')", name="ck_assistant_feedback_type"),
        CheckConstraint("adjudication_status = 'pending'", name="ck_assistant_feedback_pending"),
        CheckConstraint("qa_message_id IS NOT NULL OR parse_run_id IS NOT NULL",
                        name="ck_assistant_feedback_origin"),
    )
    feedback_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("documents.document_id"), index=True)
    request_id: Mapped[str] = mapped_column(String(200))
    qa_message_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("qa_messages.message_id"), index=True)
    parse_run_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("parse_runs.parse_run_id"), index=True)
    content_revision_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("content_revisions.revision_id"), index=True)
    generation_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("index_generations.generation_id"), index=True)
    sample_type: Mapped[str] = mapped_column(String(40))
    adjudication_status: Mapped[str] = mapped_column(String(30), default="pending", server_default="pending")
    submitted_by: Mapped[str] = mapped_column(String(200))
    snapshot: Mapped[dict] = mapped_column(JSON)
    expected_correction: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(UTC).replace(tzinfo=None), index=True)


@event.listens_for(AssistantFeedbackSample, "before_update")
@event.listens_for(AssistantFeedbackSample, "before_delete")
def immutable_feedback(_mapper, _connection, _target):
    raise ValueError("Feedback samples are append-only; adjudication uses a separate offline record")

