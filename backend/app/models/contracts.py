"""
数据契约模型 (从D组PoC迁移)
定义解析过程中的核心数据结构
"""

from datetime import datetime
from enum import Enum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class CoordinateSystem(str, Enum):
    """坐标系统"""

    PDF_POINT = "PDF_POINT"  # PDF点坐标
    PIXEL = "PIXEL"  # 像素坐标
    NORMALIZED = "NORMALIZED"  # 归一化坐标(0-1)


class Box(BaseModel):
    """边界框"""

    x0: float
    y0: float
    x1: float
    y1: float

    @field_validator("x1")
    @classmethod
    def x1_greater_than_x0(cls, v, info):
        if "x0" in info.data and v <= info.data["x0"]:
            raise ValueError("x1 must be greater than x0")
        return v

    @field_validator("y1")
    @classmethod
    def y1_greater_than_y0(cls, v, info):
        if "y0" in info.data and v <= info.data["y0"]:
            raise ValueError("y1 must be greater than y0")
        return v


class Locator(BaseModel):
    """位置定位器"""

    coordinate_system: CoordinateSystem
    bbox: Box
    page_number: int | None = None


class EvidenceRef(BaseModel):
    """证据引用"""

    page_number: int
    locator: Locator | None = None
    location_status: str = "unavailable"
    start_offset: int | None = None
    end_offset: int | None = None
    quote: str  # 原文引用
    confidence: float = Field(ge=0.0, le=1.0)


class ExtractedField(BaseModel):
    """提取的字段"""

    field_key: str
    field_value: str | None = None
    extraction_method: str  # keyword, regex, llm, vision, multipath
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: EvidenceRef | None = None


class RunStatus(str, Enum):
    """解析运行状态"""

    PENDING = "PENDING"  # 待处理
    RUNNING = "RUNNING"  # 运行中
    COMPLETED = "COMPLETED"  # 已完成
    FAILED = "FAILED"  # 失败
    CANCELLED = "CANCELLED"  # 已取消


class ParseRequest(BaseModel):
    """解析请求"""

    document_id: UUID
    checklist_type: str | None = None
    force_refresh: bool = False


class ParseResponse(BaseModel):
    """解析响应"""

    parse_run_id: UUID
    document_id: UUID
    status: RunStatus
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_seconds: float | None = None
    fields_extracted: int = 0
    error_message: str | None = None


class TextBlock(BaseModel):
    """文本块"""

    text: str
    locator: Locator | None = None
    location_status: str = "unavailable"
    # None when the OCR service reports no score (region OCR, plan C1); never a fabricated 1.0.
    confidence: float | None = Field(ge=0.0, le=1.0, default=1.0)
    label: str | None = None  # layout label of a region, e.g. "table" (plan C1)


class PageInspection(BaseModel):
    """页面检测结果"""

    page_number: int
    char_count: int
    image_count: int
    needs_ocr: bool
    is_text_heavy: bool
    is_scan: bool


class QualityIssue(BaseModel):
    """质量问题"""

    code: str  # LOW_OCR_CONFIDENCE, MISSING_FIELD, etc.
    severity: Literal["INFO", "WARNING", "ERROR"]
    message: str
    page_number: int | None = None


class PageQuality(BaseModel):
    """页面质量评估"""

    page_number: int
    status: Literal["QUALITY_OK", "DEGRADED", "REVIEW_REQUIRED"]
    mean_confidence: float | None = None
    p10_confidence: float | None = None  # 10分位数
    low_confidence_ratio: float
    issues: list[QualityIssue] = []
