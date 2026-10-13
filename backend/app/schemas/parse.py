"""Schemas for the document parsing API."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class ExtractionRequest(BaseModel):
    field_keys: list[str] | None = Field(None, max_length=1000)
    force_refresh: bool = False
    rule_bundle_id: UUID | None = None
    agent_limits: dict[str, int | float | bool | str] | None = None
    extraction_engine: Literal["rules", "checklist_agent"] | None = None

    @field_validator("agent_limits")
    @classmethod
    def bounded_agent_limits(cls, value):
        allowed = {"max_rounds": 500, "max_rounds_per_batch": 100, "max_actions_per_round": 20,
                   "max_view_pages": 1000, "max_region_views": 1000, "max_tokens": 32000,
                   "max_total_tokens": 2000000, "max_context_chars": 200000,
                   "field_batch_size": 100, "max_search_results": 100,
                   "history_images": 5, "render_dpi": 300, "region_render_dpi": 300,
                   # Plan B/C budgets (2026-10-10).
                   "min_output_tokens": 20000, "field_token_budget": 200000,
                   "max_calls_per_field": 10, "candidates_per_call": 50,
                   "max_candidates_per_field": 200, "max_candidates_multi": 300,
                   "max_images_per_call": 30, "max_image_tokens_per_call": 40000,
                   "candidate_window_chars": 1000}
        float_bounds = {"token_per_char": (0.1, 5.0), "image_tokens_per_kpx": (0.1, 10.0)}
        enums = {"agent_mode": {"legacy", "retrieval"},
                 "retrieval_image_policy": {"auto", "always"}}
        if value is not None:
            for key, count in value.items():
                if key == "thinking_enabled":
                    if not isinstance(count, bool):
                        raise ValueError("thinking_enabled 必须为布尔值")
                elif key in enums:
                    if not isinstance(count, str) or count not in enums[key]:
                        raise ValueError("不支持的核对模式或图片策略")
                elif key in float_bounds:
                    low, high = float_bounds[key]
                    if isinstance(count, bool) or not low <= float(count) <= high:
                        raise ValueError("不支持的预算键或超出平台预算边界")
                elif (key not in allowed or isinstance(count, bool | str)
                      or not 1 <= count <= allowed[key]):
                    raise ValueError("不支持的预算键或超出平台预算边界")
        return value


class StartParseResponse(BaseModel):
    parse_run_id: UUID
    document_id: UUID
    status: str
    message: str
    extraction_engine: str | None = None
    artifact_id: UUID | None = None
    parent_parse_run_id: UUID | None = None
    mode: str | None = None
    cache_status: str | None = None
    artifact_status: str | None = None


class ReliableUploadResponse(BaseModel):
    """A new reliable upload may need content preparation before a run exists."""

    contract_version: Literal["reliable-extraction-v2"]
    extraction_engine: Literal["reliable"]
    document_id: UUID
    parse_run_id: UUID | None = None
    build_id: UUID | None = None
    status: str
    message: str
    automatic_complete: bool = False


class ParseFieldResponse(BaseModel):
    field_key: str
    field_name: str
    field_value: str | None = None
    extraction_method: str
    confidence: float | None = None
    data_type: str | None = None
    unit: str | None = None
    required: bool = False
    validation_status: str | None = None
    instances: list[dict[str, Any]] | None = None
    rule_id: UUID | None = None
    rule_version: int | None = None
    extraction_detail: dict[str, Any] | None = None
    evidence: dict[str, Any] | None = None


class FactConfidence(BaseModel):
    score: float | None = Field(None, ge=0, le=1)
    kind: str = "unknown"
    version: str = "fact-contract-v1"
    calibration_scope: dict[str, Any] | None = None


class FactResponse(BaseModel):
    fact_id: str
    field_key: str
    field_name: str
    raw: str
    raw_values: list[str] = Field(default_factory=list)
    normalized: str
    value: str
    unit: str | None = None
    dimensions: dict[str, Any] = Field(default_factory=dict)
    value_kind: str = "unknown"
    source_role: str = "unknown"
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    confidence: FactConfidence = Field(
        default_factory=lambda: FactConfidence(score=None, kind="unknown")
    )
    checks: dict[str, Any] = Field(default_factory=dict)
    risk: list[dict[str, Any]] = Field(default_factory=list)
    coverage: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)
    available_for_downstream: bool | None = None
    review_status: str = "UNREVIEWED"
    field_review_status: str = "UNREVIEWED"
    # Reliable extraction v2 fields. Optional defaults preserve the v1 response contract.
    contract_version: str = "fact-contract-v1"
    assertions: dict[str, Any] = Field(default_factory=dict)
    automatic_quality_status: str | None = None
    coverage_complete: bool | None = None
    field_search_status: str | None = None
    source_validity: str = "valid"
    invalidation_reason: str | None = None
    availability_reasons: list[dict[str, Any]] = Field(default_factory=list)
    human_review_status: str = "UNREVIEWED"


class FactViewResponse(BaseModel):
    contract_version: str = "fact-contract-v1"
    parse_run_id: UUID
    document_id: UUID
    view: Literal["automatic", "reviewed", "available"]
    facts: list[FactResponse] = Field(default_factory=list)
    reviews: list[dict[str, Any]] = Field(default_factory=list)
    missing_fields: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    availability_policy: str = "structural_v1"
    availability_meaning: str = (
        "Source-linked facts for downstream assessment; not business approval."
    )
    schema_version: str = "v1"


class ParseRunDetailResponse(BaseModel):
    parse_run_id: UUID
    document_id: UUID
    status: str
    contract_version: str | None = None
    content_revision_id: UUID | None = None
    execution_generation: int | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_seconds: float | None = None
    fields_extracted: int = 0
    mean_confidence: float | None = None
    error_message: str | None = None
    document_name: str | None = None
    file_hash: str | None = None
    mine_name: str | None = None
    checklist_type: str | None = None
    reporting_period: str | None = None
    quality_status: str | None = None
    extraction_profile: dict[str, Any] | None = None
    missing_fields: list[dict[str, Any]] = []
    warnings: list[str] = []
    fields: list[ParseFieldResponse] = []
    facts: list[FactResponse] = Field(default_factory=list)
    artifact_id: UUID | None = None
    parent_parse_run_id: UUID | None = None
    mode: str | None = None
    cache_status: str | None = None
    artifact_status: str | None = None


class ParseResultItem(BaseModel):
    document_id: UUID
    file_name: str
    mine_id: str | None = None
    mine_name: str | None = None
    checklist_type: str | None = None
    reporting_period: str | None = None
    parse_run_id: UUID
    status: str
    fields_extracted: int = 0
    mean_confidence: float | None = None
    duration_seconds: float | None = None
    created_at: datetime
    file_hash: str | None = None


class ParseResultResponse(BaseModel):
    total: int
    items: list[ParseResultItem]
