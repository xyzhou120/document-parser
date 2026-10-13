"""绑定到内容与索引版本的文档助手会话。"""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.database import Base


def utcnow() -> datetime:
    return datetime.utcnow()


class QASession(Base):
    __tablename__ = "qa_sessions"

    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("documents.document_id"), index=True)
    title: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    active_revision_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("content_revisions.revision_id"), index=True)
    active_generation_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("index_generations.generation_id"), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class QAMessage(Base):
    __tablename__ = "qa_messages"
    __table_args__ = (
        UniqueConstraint("session_id", "client_message_id", name="uq_qa_message_client_id"),
        CheckConstraint("generation_fence >= 0", name="ck_qa_message_generation_fence"),
    )

    message_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("qa_sessions.session_id"), index=True)
    parent_message_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("qa_messages.message_id"), nullable=True, index=True)
    client_message_id: Mapped[str] = mapped_column(String(200))
    question: Mapped[str] = mapped_column(Text)
    mode: Mapped[str] = mapped_column(String(50), default="answer", server_default="answer")
    revision_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("content_revisions.revision_id"), index=True)
    generation_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("index_generations.generation_id"), nullable=True, index=True)
    scope_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    execution_state: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    answer: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    claims: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    citations: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    error: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    checkpoint: Mapped[dict] = mapped_column(JSON, default=dict)
    generation_fence: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class DocumentCard(Base):
    __tablename__ = "document_cards"
    __table_args__ = (UniqueConstraint("revision_id", "generation_id", name="uq_document_card_revision_generation"),)

    card_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("documents.document_id"), index=True)
    revision_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("content_revisions.revision_id"), index=True)
    generation_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("index_generations.generation_id"), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    outline: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    risks: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    citations: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    error: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
