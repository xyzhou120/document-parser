"""Source revision commands and impact/read models."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SourceRevisionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_id: UUID
    parent_artifact_id: UUID
    expected_parent_revision_id: UUID | None = None
    expected_file_hash: str = Field(min_length=32, max_length=128)
    page_number: int = Field(ge=1)
    bbox: dict[str, float] | None = None
    start_offset: int | None = Field(default=None, ge=0)
    end_offset: int | None = Field(default=None, ge=0)
    original_text: str | None = Field(default=None, max_length=20000)
    original_text_hash: str | None = Field(default=None, min_length=64, max_length=64)
    replacement_text: str | None = Field(default=None, max_length=20000)
    revision_type: Literal["CORRECT", "CONFIRM", "UNREADABLE"]
    comment: str = Field(default="", max_length=4000)
    affected_fields: list[str] = Field(default_factory=list, max_length=500)
    created_by: str = Field(min_length=1, max_length=100)
    start_extraction: bool = False
    parent_run_id: UUID | None = None
    rule_bundle_id: UUID | None = None

    @model_validator(mode="after")
    def validate_payload(self):
        if self.parent_run_id and self.rule_bundle_id:
            raise ValueError("父运行快照与规则 bundle 只能选择一个")
        if self.revision_type == "CORRECT":
            if self.start_offset is None or self.end_offset is None or self.original_text is None:
                raise ValueError("CORRECT 必须提供原文和精确 text offset")
            if self.end_offset <= self.start_offset:
                raise ValueError("end_offset 必须大于 start_offset")
            if self.replacement_text is None:
                raise ValueError("CORRECT 必须提供 replacement_text")
        if self.revision_type == "UNREADABLE" and not self.comment.strip():
            raise ValueError("UNREADABLE 必须说明不可读原因")
        if self.revision_type == "CONFIRM" and self.start_extraction:
            # Confirmation does not change text; callers can opt into a full rerun explicitly.
            return self
        return self


class SourceExtractionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    parent_run_id: UUID | None = None
    rule_bundle_id: UUID | None = None

    @model_validator(mode="after")
    def exclusive_snapshot(self):
        if self.parent_run_id and self.rule_bundle_id:
            raise ValueError("父运行快照与规则 bundle 只能选择一个")
        return self


class SourceRevisionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    revision_id: UUID
    document_id: UUID
    parent_artifact_id: UUID
    derived_artifact_id: UUID | None
    parent_revision_id: UUID | None
    revision_type: str
    page_number: int
    bbox: dict[str, Any] | None
    start_offset: int | None
    end_offset: int | None
    original_text: str | None
    original_text_hash: str | None
    replacement_text: str | None
    comment: str
    affected_fields: list[str]
    source_review_status: str
    extraction_run_id: UUID | None
    extraction_task_id: str | None
    created_by: str
    created_at: datetime


class ArtifactHistoryResponse(BaseModel):
    artifact_id: UUID
    document_id: UUID
    file_hash: str
    status: str
    source_review_status: str
    parent_artifact_id: UUID | None
    parent_revision_id: UUID | None
    revision_ids: list[UUID]
    created_at: datetime


class SourceImpactResponse(BaseModel):
    revision_id: UUID
    source_review_status: str
    affected_fields: list[str]
    extraction_plan: dict[str, Any]
    stale_run_ids: list[UUID]
