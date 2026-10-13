"""显式的助手辅助命令；每个操作都会绑定来源版本。"""

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class AuxiliaryCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DocumentCardRequest(AuxiliaryCommand):
    request_id: str = Field(min_length=1, max_length=200)
    revision_id: UUID | None = None
    generation_id: UUID | None = None

    @field_validator("request_id")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("request_id不能为空")
        return value.strip()


class AssistantCompareRequest(AuxiliaryCommand):
    parse_run_id: UUID
    view: Literal["automatic", "human"] = "automatic"


class FeedbackLocator(AuxiliaryCommand):
    page_number: int = Field(ge=1)
    block_id: str | None = Field(default=None, max_length=300)
    citation_id: str | None = Field(default=None, max_length=300)
    bbox: list[float] | None = Field(default=None, min_length=4, max_length=4)
    coordinate_system: Literal["PDF_POINT", "PIXEL", "NORMALIZED"] | None = None
    quote: str | None = Field(default=None, max_length=20_000)


class AssistantFeedbackRequest(AuxiliaryCommand):
    request_id: str = Field(min_length=1, max_length=200)
    expected_correction: dict[str, Any]
    reason: str = Field(min_length=1, max_length=4000)
    submitted_by: str = Field(min_length=1, max_length=200)
    parse_run_id: UUID | None = None
    field_key: str | None = Field(default=None, max_length=200)
    locator: FeedbackLocator | None = None
    image_hash: str | None = Field(default=None, pattern=r"^[a-fA-F0-9]{64}$")

    @field_validator("request_id", "reason", "submitted_by")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("反馈内容不能为空")
        return value.strip()

    @model_validator(mode="after")
    def validate_feedback(self):
        if self.field_key and not self.parse_run_id:
            raise ValueError("字段反馈必须指定parse_run_id")
        if not self.expected_correction:
            raise ValueError("必须提供期望修正，提交后仍需离线裁决")
        return self
