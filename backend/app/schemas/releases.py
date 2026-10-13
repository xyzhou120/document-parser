"""Explicit configuration release commands; publication is not quality certification."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class DraftBundlePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    checklist_type: str = Field(min_length=1, max_length=100)
    # Omitted rules capture the current CRUD editing source.
    rules: list[dict[str, Any]] | None = Field(default=None, max_length=1000)
    operator: str = Field(min_length=1, max_length=100)
    note: str = Field(default="", max_length=4000)


class ReleaseCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_generation: int = Field(ge=0)
    operator: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=4000)
    sample_run_ids: list[UUID] = Field(default_factory=list, max_length=100)
    sample_report: dict[str, Any] | None = None


class BundleResponse(BaseModel):
    bundle_id: UUID
    checklist_type: str
    version: int
    base_generation: int
    content_hash: str
    knowledge_hash: str
    rules: list[dict[str, Any]]
    note: str
    operator: str
    created_at: datetime
    is_active: bool
    has_been_published: bool
    current_generation: int


class ReleaseEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    event_id: UUID
    checklist_type: str
    bundle_id: UUID
    previous_bundle_id: UUID | None
    action: str
    generation: int
    operator: str
    reason: str
    sample_runs: list[dict[str, Any]]
    sample_report: dict[str, Any] | None
    created_at: datetime
