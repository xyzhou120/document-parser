"""
Pydantic Schemas - API请求/响应模型
"""

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel

# ============ 文档数据模式 ============


class DocumentCreate(BaseModel):
    """文档创建请求"""

    file_name: str
    original_name: str
    file_path: str
    file_size: int
    file_hash: str
    mime_type: str = "application/pdf"

    # 附件元数据（从上游携带）
    mine_id: Optional[str] = None
    mine_name: Optional[str] = None
    checklist_type: Optional[str] = None
    report_period: Optional[str] = None

    page_count: Optional[int] = None


class DocumentResponse(BaseModel):
    """文档响应"""

    document_id: UUID
    file_name: str
    original_name: str
    file_size: int
    file_hash: str
    mime_type: str

    mine_id: Optional[str] = None
    mine_name: Optional[str] = None
    checklist_type: Optional[str] = None
    report_period: Optional[str] = None

    page_count: Optional[int] = None
    parse_status: str

    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class DocumentListResponse(BaseModel):
    """文档列表响应"""

    total: int
    page: int
    page_size: int
    items: list[DocumentResponse]


# ============ 解析运行数据模式 ============


class ParseRunCreate(BaseModel):
    """解析运行创建请求"""

    document_id: UUID
    checklist_type: Optional[str] = None
    force_refresh: bool = False


class ParseRunResponse(BaseModel):
    """解析运行响应"""

    parse_run_id: UUID
    document_id: UUID
    checklist_type: Optional[str] = None
    status: str
    cache_status: Optional[str] = None

    fields_extracted: int
    mean_confidence: Optional[float] = None

    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    duration_seconds: Optional[float] = None

    error_message: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


# ============ 提取字段数据模式 ============


class ExtractedFieldResponse(BaseModel):
    """提取字段响应"""

    field_id: UUID
    field_key: str
    field_name: str
    field_value: str | None = None
    extraction_method: str
    confidence: Optional[float] = None
    evidence: Optional[dict] = None
    created_at: datetime

    class Config:
        from_attributes = True


class ParseResultResponse(BaseModel):
    """解析结果响应"""

    parse_run: ParseRunResponse
    fields: list[ExtractedFieldResponse]


# ============ 上传数据模式 ============


class UploadMetadata(BaseModel):
    """上传元数据"""

    mine_id: Optional[str] = None
    mine_name: Optional[str] = None
    checklist_type: Optional[str] = None
    report_period: Optional[str] = None


class UploadResponse(BaseModel):
    """上传响应"""

    document: DocumentResponse
    parse_run_id: Optional[UUID] = None
    message: str = "Upload successful"
