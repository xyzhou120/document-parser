"""
SQLAlchemy数据库模型
"""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, Boolean, Float, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """数据库模型基类"""

    pass


class Document(Base):
    """文档表"""

    __tablename__ = "documents"

    # 主键
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # 文件信息
    file_name: Mapped[str] = mapped_column(String(500))
    original_name: Mapped[str] = mapped_column(String(500))
    file_path: Mapped[str] = mapped_column(Text)  # MinIO路径
    file_size: Mapped[int] = mapped_column(BigInteger)
    file_hash: Mapped[str] = mapped_column(String(64), index=True)
    mime_type: Mapped[str] = mapped_column(String(100))

    # 附件元数据（从上游携带）
    mine_id: Mapped[Optional[str]] = mapped_column(String(100), index=True)
    mine_name: Mapped[Optional[str]] = mapped_column(String(200))
    checklist_type: Mapped[Optional[str]] = mapped_column(String(100), index=True)
    report_period: Mapped[Optional[str]] = mapped_column(String(50), index=True)

    # 文档属性
    page_count: Mapped[Optional[int]] = mapped_column(Integer)
    parse_status: Mapped[str] = mapped_column(String(50), default="PENDING", index=True)

    # 时间戳
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        default=datetime.utcnow, onupdate=datetime.utcnow
    )


class ParseRun(Base):
    """解析运行表"""

    __tablename__ = "parse_runs"

    # 主键
    parse_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # 关联
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    parent_parse_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), index=True)
    artifact_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), index=True)
    # 可靠提取 v2 将每次新运行绑定到不可变内容快照。
    # 这些字段对旧版 ParseRun 记录和兼容读取器保持可空。
    content_revision_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("content_revisions.revision_id"), index=True)
    contract_version: Mapped[Optional[str]] = mapped_column(String(80), index=True)
    execution_generation: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    requested_field_keys: Mapped[Optional[list]] = mapped_column(JSON)
    run_mode: Mapped[Optional[str]] = mapped_column(String(30))  # FULL / INCREMENTAL

    # 解析配置
    checklist_type: Mapped[Optional[str]] = mapped_column(String(100))
    extraction_profile: Mapped[Optional[dict]] = mapped_column(JSON)

    # 状态
    status: Mapped[str] = mapped_column(String(50), default="PENDING", index=True)
    cache_status: Mapped[Optional[str]] = mapped_column(String(50))  # CACHE_HIT, MISS, etc.

    # 结果统计
    fields_extracted: Mapped[int] = mapped_column(Integer, default=0)
    mean_confidence: Mapped[Optional[float]] = mapped_column(Float)

    # 性能指标
    started_at: Mapped[Optional[datetime]] = mapped_column()
    completed_at: Mapped[Optional[datetime]] = mapped_column()
    duration_seconds: Mapped[Optional[float]] = mapped_column(Float)

    # 综合质量状态：QUALITY_OK / REVIEW_REQUIRED / DEGRADED / FAILED
    quality_status: Mapped[Optional[str]] = mapped_column(String(30))

    # 错误信息
    error_message: Mapped[Optional[str]] = mapped_column(Text)

    # 时间戳
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow, index=True)


class ExtractedFieldModel(Base):
    """提取字段表"""

    __tablename__ = "extracted_fields"

    # 主键
    field_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # 关联
    parse_run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)

    # 字段信息
    field_key: Mapped[str] = mapped_column(String(100), index=True)
    field_name: Mapped[str] = mapped_column(String(200))
    target_type: Mapped[Optional[str]] = mapped_column(String(30))
    target_group_key: Mapped[Optional[str]] = mapped_column(String(100), index=True)
    target_status: Mapped[Optional[str]] = mapped_column(String(40), index=True)
    status_reason: Mapped[Optional[dict]] = mapped_column(JSON)
    field_value: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # 提取方法
    extraction_method: Mapped[str] = mapped_column(String(50))  # keyword、llm、vision 等
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # 证据（JSON格式存储EvidenceRef）
    evidence: Mapped[Optional[dict]] = mapped_column(JSON)

    # 规则化提取新增：字段契约、多实例与校验状态
    data_type: Mapped[Optional[str]] = mapped_column(String(20))
    unit: Mapped[Optional[str]] = mapped_column(String(50))
    required: Mapped[bool] = mapped_column(Boolean, default=False)
    instances: Mapped[Optional[list]] = mapped_column(JSON)
    rule_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), index=True)
    rule_version: Mapped[Optional[int]] = mapped_column(Integer)
    validation_status: Mapped[Optional[str]] = mapped_column(String(30))
    extraction_detail: Mapped[Optional[dict]] = mapped_column(JSON)
    # 数据分析只读这份小摘要。完整 extraction_detail 里的证据原文不参与列表统计。
    quality_inputs: Mapped[Optional[dict]] = mapped_column(JSON)

    # 时间戳
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)


class ParseLog(Base):
    """解析日志表"""

    __tablename__ = "parse_logs"

    # 主键
    log_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # 关联
    parse_run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)

    # 日志信息
    level: Mapped[str] = mapped_column(String(20))  # DEBUG、INFO、WARNING、ERROR
    stage: Mapped[str] = mapped_column(String(50))  # pdf_parse、ocr、extraction 等
    message: Mapped[str] = mapped_column(Text)
    details: Mapped[Optional[dict]] = mapped_column(JSON)

    # 时间戳
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow, index=True)


class DocumentArtifact(Base):
    """持久化的文档前处理产物。

    pages 保存 native text、表格/cell bbox 及 OCR block locator；它是不可变的
    内容寻址缓存，规则变化时只重跑字段提取。
    """

    __tablename__ = "document_artifacts"

    artifact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    file_hash: Mapped[str] = mapped_column(String(64), index=True)
    preprocess_profile_version: Mapped[str] = mapped_column(String(50), index=True)
    status: Mapped[str] = mapped_column(String(20), default="READY", index=True)
    pages: Mapped[list] = mapped_column(JSON)
    quality: Mapped[Optional[dict]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(default=datetime.utcnow, onupdate=datetime.utcnow)
