"""清单类型与提取规则的数据库模型"""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.database import Base


class ChecklistType(Base):
    """清单类型"""

    __tablename__ = "checklist_types"

    checklist_type: Mapped[str] = mapped_column(String(100), primary_key=True)
    checklist_name: Mapped[str] = mapped_column(String(255))
    description: Mapped[Optional[str]] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=100)
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        default=datetime.utcnow, onupdate=datetime.utcnow
    )


class ExtractionRule(Base):
    """提取规则"""

    __tablename__ = "extraction_rules"
    __table_args__ = (
        UniqueConstraint("checklist_type", "field_key", name="uq_rule_checklist_field"),
    )

    rule_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    checklist_type: Mapped[str] = mapped_column(String(100), index=True)
    field_key: Mapped[str] = mapped_column(String(100), index=True)
    field_name_cn: Mapped[str] = mapped_column(String(255))
    field_description: Mapped[Optional[str]] = mapped_column(Text)
    dimension_schema: Mapped[dict] = mapped_column(JSON, default=dict)
    value_kind: Mapped[str] = mapped_column(String(30), default="unknown")
    data_type: Mapped[str] = mapped_column(String(20), default="string")
    unit: Mapped[Optional[str]] = mapped_column(String(50))
    required: Mapped[bool] = mapped_column(Boolean, default=False)
    multi_instance: Mapped[bool] = mapped_column(Boolean, default=False)
    merge_strategy: Mapped[str] = mapped_column(String(30), default="value_consistent")
    strategy: Mapped[dict] = mapped_column(JSON, default=dict)
    validation: Mapped[Optional[dict]] = mapped_column(JSON)
    fallback_value: Mapped[Optional[str]] = mapped_column(String(255))
    priority: Mapped[int] = mapped_column(Integer, default=50, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        default=datetime.utcnow, onupdate=datetime.utcnow
    )


class RuleChangeLog(Base):
    """规则变更日志"""

    __tablename__ = "rule_change_log"

    log_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    rule_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), index=True)
    checklist_type: Mapped[Optional[str]] = mapped_column(String(100), index=True)
    action: Mapped[str] = mapped_column(String(30))
    before_state: Mapped[Optional[dict]] = mapped_column(JSON)
    after_state: Mapped[Optional[dict]] = mapped_column(JSON)
    operator: Mapped[Optional[str]] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow, index=True)
