"""可靠提取执行的版本化 API 数据结构。"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ExecutionJobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    job_id: UUID
    run_id: UUID
    field_key: str
    rule_hash: str
    revision_id: UUID
    execution_state: str
    semantic_state: str
    phase: str
    checkpoint: dict[str, Any] = Field(default_factory=dict)
    next_attempt_at: datetime | None = None
    lease_owner: str | None = None
    lease_until: datetime | None = None
    fence_token: int
    progress_seq: int
    attempt: int
    last_error: str | None = None


class PageProgress(BaseModel):
    """Document-level progress for the page-batched extraction strategy."""

    processed_units: int = 0
    total_units: int = 0
    processed_pages: int = 0
    total_pages: int = 0
    failed_units: int = 0
    strategy: str = "page_batch_v1"


class ExecutionResponse(BaseModel):
    contract_version: str
    run_id: UUID
    document_id: UUID
    status: str
    execution_generation: int
    automatic_complete: bool
    total_fields: int
    counts: dict[str, int] = Field(default_factory=dict)
    jobs: list[ExecutionJobResponse] = Field(default_factory=list)
    page_progress: PageProgress | None = None
    next_cursor: str | None = None
    legacy: bool = False


class FieldJobsResponse(BaseModel):
    contract_version: str
    run_id: UUID
    jobs: list[ExecutionJobResponse]
    next_cursor: str | None = None
    legacy: bool = False


class RunControlResponse(BaseModel):
    contract_version: str
    run_id: UUID
    status: str
    execution_generation: int
    resumed: bool = False


class ReliableExtractRequest(BaseModel):
    request_id: str | None = Field(default=None, min_length=1, max_length=200)
    field_keys: list[str] | None = Field(default=None, max_length=1000)
    revision_id: UUID | None = None
    parent_run_id: UUID | None = None
    document_token_hard_limit: int | None = Field(default=None, ge=1, le=10_000_000_000)
