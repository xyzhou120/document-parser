"""模型包。"""

from app.models.contracts import (
    Box,
    CoordinateSystem,
    EvidenceRef,
    ExtractedField,
    Locator,
    PageInspection,
    PageQuality,
    ParseRequest,
    ParseResponse,
    QualityIssue,
    RunStatus,
    TextBlock,
)
from app.models.database import Base, Document, ExtractedFieldModel, ParseLog, ParseRun
from app.models.execution import (
    ExecutionOutbox,
    ParseCandidate,
    ParseFieldJob,
    ParseModelCall,
    ParseRangeWorkItem,
)
from app.models.content import (
    ContentBuildJob,
    ContentBuildEntry,
    ContentImpact,
    ContentInvalidation,
    ContentRevision,
    DocumentProcessingHead,
    IndexChunk,
    IndexGeneration,
)
from app.models.assistant import DocumentCard, QAMessage, QASession
from app.models.assistant_feedback import AssistantFeedbackSample
from app.models.shared_execution import SharedExecution, SharedExecutionSubscriber
from app.models.rules import ChecklistType, ExtractionRule, RuleChangeLog
from app.models.attachments import (
    Attachment, AttachmentFileRevision, AttachmentImport, AttachmentImportRow,
    AttachmentChecklistMapping, AttachmentVersion, AttachmentVersionSummary,
)
from app.models.schemas import (
    DocumentCreate,
    DocumentListResponse,
    DocumentResponse,
    ExtractedFieldResponse,
    ParseResultResponse,
    ParseRunCreate,
    ParseRunResponse,
    UploadMetadata,
    UploadResponse,
)

__all__ = [
    # 契约模型
    "Box",
    "CoordinateSystem",
    "EvidenceRef",
    "ExtractedField",
    "Locator",
    "PageInspection",
    "PageQuality",
    "ParseRequest",
    "ParseResponse",
    "QualityIssue",
    "RunStatus",
    "TextBlock",
    # 数据库模型
    "Base",
    "Document",
    "ExtractedFieldModel",
    "ParseLog",
    "ParseRun",
    "ParseFieldJob",
    "ParseRangeWorkItem",
    "ParseCandidate",
    "ParseModelCall",
    "ExecutionOutbox",
    "ContentBuildJob",
    "ContentBuildEntry",
    "ContentRevision",
    "ContentImpact",
    "ContentInvalidation",
    "DocumentProcessingHead",
    "IndexGeneration",
    "IndexChunk",
    "QASession",
    "QAMessage",
    "DocumentCard",
    "AssistantFeedbackSample",
    "SharedExecution", "SharedExecutionSubscriber",
    # 规则模型
    "ChecklistType",
    "ExtractionRule",
    "RuleChangeLog",
    "Attachment", "AttachmentFileRevision", "AttachmentImport", "AttachmentImportRow",
    "AttachmentChecklistMapping", "AttachmentVersion", "AttachmentVersionSummary",
    # 数据模式
    "DocumentCreate",
    "DocumentListResponse",
    "DocumentResponse",
    "ExtractedFieldResponse",
    "ParseResultResponse",
    "ParseRunCreate",
    "ParseRunResponse",
    "UploadMetadata",
    "UploadResponse",
]
