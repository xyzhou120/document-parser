"""公开文档助手数据模式。"""

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class AssistantSessionCreate(BaseModel):
    request_id: str = Field(min_length=1, max_length=200)
    revision_id: UUID | None = None
    generation_id: UUID | None = None

    @field_validator("request_id")
    @classmethod
    def request_id_nonblank(cls, value):
        if not value.strip():
            raise ValueError("request_id不能为空")
        return value.strip()


class AssistantMessageCreate(BaseModel):
    request_id: str = Field(min_length=1, max_length=200)
    question: str = Field(min_length=1, max_length=20_000)
    mode: Literal["answer", "all_locations"] = "answer"
    parent_message_id: UUID | None = None

    @field_validator("request_id", "question")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("请求内容不能为空")
        return value.strip()


class AssistantSessionResponse(BaseModel):
    session_id: UUID
    conversation_id: UUID
    document_id: UUID
    revision_id: UUID
    generation_id: UUID
    status: str


class AssistantCitation(BaseModel):
    citation_id: str
    chunk_id: str
    generation_id: str
    revision_id: str
    block_id: str | None = None
    page_number: int | None = None
    bbox: list[float] | None = None
    coordinate_system: str | None = None
    quote: str
    table_ref: dict[str, Any] | None = None
    source: str | None = None
    risk: str | None = None


class AssistantClaim(BaseModel):
    text: str
    quote: str
    citation_ids: list[str]
    value: Any = None
    unit: str | None = None
    value_kind: str | None = None
    object: str | None = None
    table_relation: str | None = None
    quality: str | None = None


class AssistantMessageResponse(BaseModel):
    message_id: UUID
    session_id: UUID
    conversation_id: UUID
    client_message_id: str
    question: str
    mode: str
    revision_id: UUID
    generation_id: UUID | None
    execution_status: str
    answer: str | None = None
    claims: list[AssistantClaim] = Field(default_factory=list)
    citations: list[AssistantCitation] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    error: dict[str, Any] | None = None
    scope_snapshot: dict[str, Any] = Field(default_factory=dict)
    next_cursor: str | None = None
    search_finished: bool = False
    remaining_work: bool = False


class AssistantLocationResponse(BaseModel):
    items: list[AssistantCitation]
    next_cursor: str | None
    search_finished: bool
    remaining_work: bool
    revision_id: UUID
    generation_id: UUID | None


class AssistantStatusResponse(BaseModel):
    document_id: UUID
    can_ask: bool
    status: str
    revision_id: UUID | None = None
    generation_id: UUID | None = None
    coverage_status: str
    semantic_status: str
    answerer_status: str
    capabilities: dict[str, bool]
