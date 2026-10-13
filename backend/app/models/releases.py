"""不可变配置包与只追加的发布决策。"""

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint, event
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.database import Base


class RuleBundle(Base):
    __tablename__ = "rule_bundles"
    __table_args__ = (UniqueConstraint("checklist_type", "version", name="uq_bundle_version"),)

    bundle_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    checklist_type: Mapped[str] = mapped_column(String(100), index=True)
    version: Mapped[int] = mapped_column(Integer)
    base_generation: Mapped[int] = mapped_column(Integer)
    rules: Mapped[list] = mapped_column(JSON)
    content_hash: Mapped[str] = mapped_column(String(64))
    knowledge_hash: Mapped[str] = mapped_column(String(64))
    note: Mapped[str] = mapped_column(Text, default="")
    operator: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)


class ChecklistReleaseHead(Base):
    __tablename__ = "checklist_release_heads"

    checklist_type: Mapped[str] = mapped_column(String(100), primary_key=True)
    bundle_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("rule_bundles.bundle_id"))
    generation: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(default=datetime.utcnow, onupdate=datetime.utcnow)


class RuleReleaseEvent(Base):
    __tablename__ = "rule_release_events"

    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    checklist_type: Mapped[str] = mapped_column(String(100), index=True)
    bundle_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("rule_bundles.bundle_id"), index=True)
    previous_bundle_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    action: Mapped[str] = mapped_column(String(20))
    generation: Mapped[int] = mapped_column(Integer)
    operator: Mapped[str] = mapped_column(String(100))
    reason: Mapped[str] = mapped_column(Text)
    sample_runs: Mapped[list] = mapped_column(JSON, default=list)
    sample_report: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow, index=True)


def _reject_mutation(mapper, connection, target):
    raise ValueError(f"{target.__tablename__} is append-only")


for _model in (RuleBundle, RuleReleaseEvent):
    event.listen(_model, "before_update", _reject_mutation)
    event.listen(_model, "before_delete", _reject_mutation)


# init_db 在 PostgreSQL 中也应执行这些语句，与 ORM 防护保持一致。
APPEND_ONLY_DDL = [
    """CREATE OR REPLACE FUNCTION reject_rule_release_mutation() RETURNS trigger
    LANGUAGE plpgsql AS $$ BEGIN
      RAISE EXCEPTION 'rule release records are append-only';
    END; $$""",
    *[
        f"""DO $$ BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = '{table}_append_only') THEN
            CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION reject_rule_release_mutation();
          END IF;
        END; $$"""
        for table in ("rule_bundles", "rule_release_events")
    ],
]
