"""Human review contracts. A human assertion is never an automatic confidence score."""

import math
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ReviewEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page_number: int = Field(ge=1)
    bbox: list[float] | None = Field(default=None, min_length=4, max_length=4)
    coordinate_system: Literal["PDF_POINT"] = "PDF_POINT"
    evidence_id: str | None = Field(default=None, max_length=100)
    role: str = Field(default="value", min_length=1, max_length=100)
    quote: str | None = Field(default=None, max_length=6000)

    @field_validator("bbox", mode="before")
    @classmethod
    def finite_coordinates(cls, value):
        if value is not None and any(isinstance(x, bool) or not math.isfinite(float(x)) for x in value):
            raise ValueError("证据坐标必须为有限数字")
        return value


class ReviewedFact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    raw_value: str = Field(min_length=1, max_length=12000)
    value: str = Field(min_length=1, max_length=12000)
    unit: str | None = Field(..., max_length=100)
    value_kind: str = Field(min_length=1, max_length=100)
    source_role: str = Field(min_length=1, max_length=100)
    dimensions: dict[str, str | None]
    evidence: list[ReviewEvidence] = Field(min_length=1, max_length=100)

    @field_validator("raw_value", "value", "value_kind", "source_role")
    @classmethod
    def meaningful_content(cls, value):
        if not value.strip():
            raise ValueError("事实内容与来源语义不能为空")
        return value

    @field_validator("dimensions")
    @classmethod
    def bounded_dimensions(cls, value):
        if len(value) > 30 or any(not k.strip() or len(k) > 100 or (v is not None and len(v) > 1000) for k, v in value.items()):
            raise ValueError("维度标识或内容超出允许范围")
        return value


class FieldReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["ACCEPT", "CORRECT", "REJECT"]
    expected_revision: int = Field(ge=0)
    reviewer: str = Field(min_length=1, max_length=200)
    reason: str = Field(default="", max_length=6000)
    file_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    facts: list[ReviewedFact] = Field(default_factory=list, max_length=1000)

    @model_validator(mode="after")
    def validate_action(self):
        self.reviewer = self.reviewer.strip()
        self.reason = self.reason.strip()
        if not self.reviewer:
            raise ValueError("复核人不能为空")
        if self.action in {"CORRECT", "REJECT"} and not self.reason:
            raise ValueError("校正或拒绝必须填写原因")
        if self.action == "CORRECT" and not self.facts:
            raise ValueError("校正必须提交有证据的事实")
        if self.action != "CORRECT" and self.facts:
            raise ValueError("仅校正操作允许提交facts")
        return self


class FieldReviewResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    review_id: UUID
    parse_run_id: UUID
    document_id: UUID
    field_key: str
    revision: int
    previous_revision: int
    action: str
    reviewer: str
    reason: str
    file_hash: str
    before_snapshot: dict
    facts: list[dict]
    created_at: datetime
    review_status: str = "HUMAN_REVIEWED"
