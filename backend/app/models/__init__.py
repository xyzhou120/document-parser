"""Models package"""

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
    ContentImpact,
    ContentInvalidation,
    ContentRevision,
    DocumentProcessingHead,
    IndexChunk,
    IndexGeneration,
)
from app.models.assistant import DocumentCard, QAMessage, QASession
from app.models.assistant_feedback import AssistantFeedbackSample
from app.models.rules import ChecklistType, ExtractionRule, RuleChangeLog
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
    # Contracts
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
    # Database
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
    # Rules
    "ChecklistType",
    "ExtractionRule",
    "RuleChangeLog",
    # Schemas
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
