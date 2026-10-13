"""只追加的人工复核修订，与自动提取记录分离。"""

import uuid
from datetime import datetime

from sqlalchemy import Integer, String, Text, UniqueConstraint, event
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.database import Base


class FieldReviewRevision(Base):
    __tablename__ = "field_review_revisions"
    __table_args__ = (
        UniqueConstraint("parse_run_id", "field_key", "revision", name="uq_field_review_revision"),
    )

    review_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    parse_run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    field_key: Mapped[str] = mapped_column(String(100), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    previous_revision: Mapped[int] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(20))
    reviewer: Mapped[str] = mapped_column(String(200))
    reason: Mapped[str] = mapped_column(Text)
    file_hash: Mapped[str] = mapped_column(String(64))
    before_snapshot: Mapped[dict] = mapped_column(JSON)
    facts: Mapped[list] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow, index=True)


@event.listens_for(FieldReviewRevision, "before_update")
@event.listens_for(FieldReviewRevision, "before_delete")
def immutable_revision(_mapper, _connection, _target):
    raise ValueError("Human review revisions are append-only")
