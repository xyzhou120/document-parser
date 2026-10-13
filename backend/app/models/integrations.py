"""Durable inbound idempotency records and outbound delivery receipts."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSON
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.database import Base


def utcnow():
    return datetime.now(UTC).replace(tzinfo=None)


class IntegrationRequest(Base):
    __tablename__ = "integration_requests"

    event_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    document_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), index=True)
    parse_run_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), index=True)
    payload_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String(30), default="PENDING")
    actor: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class IntegrationOutbox(Base):
    __tablename__ = "integration_outbox"

    event_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    parse_run_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), index=True)
    document_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    destination: Mapped[str] = mapped_column(String(1000))
    state: Mapped[str] = mapped_column(String(30), default="PENDING", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    acknowledgement: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
