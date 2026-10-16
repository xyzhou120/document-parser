"""规则管理 API 的请求/响应模型"""

import re
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.rules.semantic_contract import (
    RetrievalPolicy,
    RuleSemanticContract,
    normalize_rule_semantics,
)

ALLOWED_DATA_TYPES = {"string", "integer", "float", "date", "array", "json"}
ALLOWED_MERGE_STRATEGIES = {"value_consistent", "keep_all", "most_recent"}
ALLOWED_METHODS = {"keyword_search", "table_lookup", "llm_extract", "regex", "agent_extract"}
ALLOWED_VALUE_KINDS = {
    "original",
    "design",
    "actual",
    "calculated",
    "measured",
    "threshold",
    "reported",
    "presence",
    "process",
    "unknown",
}
ALLOWED_TARGET_TYPES = {"fact", "statement", "table"}


class DimensionSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    required: bool = False
    repeatable: bool = False
    parent_key: str | None = None
    description: str = Field(default="", max_length=1000)


class TableColumnSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    header: str = Field(min_length=1, max_length=255)
    aliases: list[str] = Field(default_factory=list)
    data_type: str = "string"
    unit: str | None = None
    required: bool = True

    @field_validator("data_type")
    @classmethod
    def valid_data_type(cls, value):
        if value not in ALLOWED_DATA_TYPES:
            raise ValueError("表格列数据类型无效")
        return value


class TableOutputSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    row_identity: list[str] = Field(min_length=1)
    columns: list[TableColumnSpec] = Field(min_length=1)

    @model_validator(mode="after")
    def valid_identity(self):
        keys = [column.key for column in self.columns]
        if len(keys) != len(set(keys)):
            raise ValueError("表格列标识不能重复")
        if set(self.row_identity) - set(keys):
            raise ValueError("行身份必须引用已配置的表格列")
        return self


class StatementOutputSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    polarity: str = "any"

    @field_validator("polarity")
    @classmethod
    def valid_polarity(cls, value):
        if value not in {"affirmed", "negated", "uncertain", "unknown", "any"}:
            raise ValueError("原文目标的 polarity 配置无效")
        return value


class AgentKnowledge(BaseModel):
    """字段的版本化背景、说明和可审计检索提示。"""

    model_config = ConfigDict(extra="forbid")
    background: str = Field(default="", max_length=4000)
    instructions: str = Field(default="", max_length=6000)
    synonyms: list[str] = Field(default_factory=list, max_length=60)
    negative_examples: list[str] = Field(default_factory=list, max_length=30)
    identity_dimensions: list[str] = Field(default_factory=list, max_length=30)
    evidence_requirements: list[str] = Field(default_factory=list, max_length=30)
    source_roles: list[str] = Field(default_factory=list, max_length=30)
    manual_references: list[str] = Field(default_factory=list, max_length=30)
    retrieval: RetrievalPolicy | None = None

    @field_validator(
        "synonyms",
        "negative_examples",
        "identity_dimensions",
        "evidence_requirements",
        "source_roles",
        "manual_references",
    )
    @classmethod
    def nonblank_items(cls, value):
        if any(not x.strip() or len(x) > 1000 for x in value):
            raise ValueError("知识列表需使用不超过1000字的非空字符串")
        return list(dict.fromkeys(value))


def validate_dimensions(schema: dict[str, DimensionSpec]) -> dict[str, DimensionSpec]:
    for key, definition in schema.items():
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", key):
            raise ValueError("维度标识需以小写字母开头，仅含小写字母、数字与下划线")
        if definition.parent_key is not None and definition.parent_key not in schema:
            raise ValueError(f"维度 {key} 的父维度不存在")
        seen = {key}
        parent = definition.parent_key
        while parent is not None:
            if parent in seen:
                raise ValueError("维度父子关系不能形成循环")
            seen.add(parent)
            parent = schema[parent].parent_key
    return schema


def validate_strategy(strategy: dict[str, Any]) -> dict[str, Any]:
    methods = strategy.get("methods", [])
    if not isinstance(methods, list) or any(
        not isinstance(method, str) or method not in ALLOWED_METHODS for method in methods
    ):
        raise ValueError(f"提取方法需为 {sorted(ALLOWED_METHODS)} 之一")
    if "agent_extract" in strategy:
        strategy = {
            **strategy,
            "agent_extract": AgentKnowledge.model_validate(strategy["agent_extract"]).model_dump(),
        }
    if "semantic_contract" in strategy:
        contract = RuleSemanticContract.model_validate(strategy["semantic_contract"]).model_dump()
        strategy = {**strategy, "semantic_contract": contract}
    for key, pattern_key in (("keyword_search", "regex"), ("regex", "pattern")):
        config = strategy.get(key)
        if config is not None:
            # 历史顶层 regex 字符串配置仍可导入。
            pattern = config if key == "regex" and isinstance(config, str) else None
            if isinstance(config, dict):
                pattern = config.get(pattern_key)
            elif pattern is None:
                raise ValueError(f"{key} 必须是对象")
            if pattern is not None:
                try:
                    re.compile(pattern)
                except (re.error, TypeError) as exc:
                    raise ValueError(f"{key} 正则无效: {exc}") from exc
    bindings = strategy.get("dimension_bindings", {})
    if not isinstance(bindings, dict):
        raise ValueError("dimension_bindings 必须是对象")
    for key, config in bindings.items():
        if not isinstance(config, dict):
            raise ValueError(f"维度绑定 {key} 必须是对象")
        pattern = config.get("regex")
        columns = config.get("column_keywords")
        if pattern is None and not columns:
            raise ValueError(f"维度绑定 {key} 必须配置 regex 或 column_keywords")
        if columns is not None and (
            not isinstance(columns, list)
            or any(not isinstance(column, str) or not column.strip() for column in columns)
        ):
            raise ValueError(f"维度绑定 {key} 的 column_keywords 必须是非空字符串列表")
        if pattern is not None:
            try:
                re.compile(pattern)
            except (re.error, TypeError) as exc:
                raise ValueError(f"维度绑定 {key} 正则无效: {exc}") from exc
    return strategy


class ChecklistTypePayload(BaseModel):
    """清单类型新建/修改"""

    checklist_type: str = Field(min_length=1, max_length=100)
    checklist_name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    is_active: bool = True
    sort_order: int = 100

    @field_validator("checklist_type")
    @classmethod
    def validate_key(cls, value: str) -> str:
        import re

        if not re.fullmatch(r"[a-z][a-z0-9_]{1,99}", value):
            raise ValueError("清单标识需以小写字母开头，仅含小写字母、数字与下划线")
        return value


class ChecklistTypePatch(BaseModel):
    """清单类型部分更新"""

    checklist_name: str | None = Field(default=None, max_length=255)
    description: str | None = None
    is_active: bool | None = None
    sort_order: int | None = None


class ChecklistTypeResponse(BaseModel):
    checklist_type: str
    checklist_name: str
    description: str | None = None
    is_active: bool = True
    sort_order: int = 100
    rule_count: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None


class RulePayload(BaseModel):
    """规则新建/修改"""

    checklist_type: str = Field(min_length=1, max_length=100)
    field_key: str = Field(min_length=1, max_length=100)
    field_name_cn: str = Field(min_length=1, max_length=255)
    target_type: str = "fact"
    target_group_key: str | None = Field(default=None, max_length=100)
    target_group_name: str | None = Field(default=None, max_length=255)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    source_policy: dict[str, Any] = Field(default_factory=dict)
    evidence_policy: dict[str, Any] = Field(default_factory=dict)
    field_description: str | None = None
    dimension_schema: dict[str, DimensionSpec] = Field(default_factory=dict)
    value_kind: str = "unknown"
    data_type: str = "string"
    unit: str | None = Field(default=None, max_length=50)
    required: bool = False
    multi_instance: bool = False
    merge_strategy: str = "value_consistent"
    strategy: dict[str, Any] = Field(default_factory=dict)
    validation: dict[str, Any] | None = None
    fallback_value: str | None = Field(default=None, max_length=255)
    priority: int = 50
    enabled: bool = True

    @model_validator(mode="after")
    def compatible_grouping_dimensions(self):
        keys = (self.strategy.get("agent_extract") or {}).get("identity_dimensions", [])
        if set(keys) - set(self.dimension_schema):
            raise ValueError("智能体归并身份维度必须在 dimension_schema 中声明")
        normalize_rule_semantics(self.model_dump())
        return self

    @field_validator("dimension_schema")
    @classmethod
    def validate_dimension_schema(cls, value):
        return validate_dimensions(value)

    @field_validator("value_kind")
    @classmethod
    def validate_value_kind(cls, value):
        if value not in ALLOWED_VALUE_KINDS:
            raise ValueError(f"值类型需为 {sorted(ALLOWED_VALUE_KINDS)} 之一")
        return value

    @field_validator("target_type")
    @classmethod
    def validate_target_type(cls, value):
        if value not in ALLOWED_TARGET_TYPES:
            raise ValueError(f"目标类型需为 {sorted(ALLOWED_TARGET_TYPES)} 之一")
        return value

    @model_validator(mode="after")
    def validate_target_schema(self):
        if self.target_type == "table":
            if self.enabled or self.output_schema:
                self.output_schema = TableOutputSpec.model_validate(self.output_schema).model_dump()
        if self.target_type == "statement":
            self.output_schema = StatementOutputSpec.model_validate(self.output_schema).model_dump()
        for name, policy in (("source_policy", self.source_policy), ("evidence_policy", self.evidence_policy)):
            if not isinstance(policy, dict):
                raise ValueError(f"{name} 必须是对象")
        return self

    @field_validator("strategy")
    @classmethod
    def validate_strategy_config(cls, value):
        return validate_strategy(value)

    @field_validator("field_key")
    @classmethod
    def validate_field_key(cls, value: str) -> str:
        import re

        if not re.fullmatch(r"[a-z][a-z0-9_]{1,99}", value):
            raise ValueError("字段标识需以小写字母开头，仅含小写字母、数字与下划线")
        return value

    @field_validator("data_type")
    @classmethod
    def validate_data_type(cls, value: str) -> str:
        if value not in ALLOWED_DATA_TYPES:
            raise ValueError(f"数据类型需为 {sorted(ALLOWED_DATA_TYPES)} 之一")
        return value

    @field_validator("merge_strategy")
    @classmethod
    def validate_merge(cls, value: str) -> str:
        if value not in ALLOWED_MERGE_STRATEGIES:
            raise ValueError(f"合并策略需为 {sorted(ALLOWED_MERGE_STRATEGIES)} 之一")
        return value

    @field_validator("priority")
    @classmethod
    def validate_priority(cls, value: int) -> int:
        if not 0 <= value <= 100:
            raise ValueError("优先级取值范围为 0-100")
        return value


class RulePatch(BaseModel):
    """规则部分更新"""

    field_key: str | None = None
    field_name_cn: str | None = Field(default=None, max_length=255)
    target_type: str | None = None
    target_group_key: str | None = Field(default=None, max_length=100)
    target_group_name: str | None = Field(default=None, max_length=255)
    output_schema: dict[str, Any] | None = None
    source_policy: dict[str, Any] | None = None
    evidence_policy: dict[str, Any] | None = None
    field_description: str | None = None
    dimension_schema: dict[str, DimensionSpec] | None = None
    value_kind: str | None = None
    data_type: str | None = None
    unit: str | None = Field(default=None, max_length=50)
    required: bool | None = None
    multi_instance: bool | None = None
    merge_strategy: str | None = None
    strategy: dict[str, Any] | None = None
    validation: dict[str, Any] | None = None
    fallback_value: str | None = Field(default=None, max_length=255)
    priority: int | None = None
    enabled: bool | None = None

    @field_validator("field_key", "field_name_cn", "data_type", "merge_strategy", mode="before")
    @classmethod
    def reject_null_for_non_nullable(cls, value):
        if value is None:
            raise ValueError("该字段不能为 null")
        return value

    @field_validator(
        "required",
        "multi_instance",
        "strategy",
        "priority",
        "enabled",
        "dimension_schema",
        "value_kind",
        mode="before",
    )
    @classmethod
    def reject_null(cls, value):
        if value is None:
            raise ValueError("该字段不能为 null")
        return value

    @field_validator("dimension_schema")
    @classmethod
    def validate_dimension_schema(cls, value):
        return validate_dimensions(value)

    @field_validator("value_kind")
    @classmethod
    def validate_value_kind(cls, value):
        return RulePayload.validate_value_kind(value)

    @field_validator("target_type")
    @classmethod
    def validate_patch_target_type(cls, value):
        if value is not None and value not in ALLOWED_TARGET_TYPES:
            raise ValueError(f"目标类型需为 {sorted(ALLOWED_TARGET_TYPES)} 之一")
        return value

    @field_validator("strategy")
    @classmethod
    def validate_strategy_config(cls, value):
        return validate_strategy(value)

    @field_validator("field_key")
    @classmethod
    def validate_field_key(cls, value):
        return RulePayload.validate_field_key(value)

    @field_validator("priority")
    @classmethod
    def validate_priority(cls, value):
        return RulePayload.validate_priority(value)

    @field_validator("data_type")
    @classmethod
    def validate_data_type(cls, value: str | None) -> str | None:
        if value is not None and value not in ALLOWED_DATA_TYPES:
            raise ValueError(f"数据类型需为 {sorted(ALLOWED_DATA_TYPES)} 之一")
        return value

    @field_validator("merge_strategy")
    @classmethod
    def validate_merge(cls, value: str | None) -> str | None:
        if value is not None and value not in ALLOWED_MERGE_STRATEGIES:
            raise ValueError(f"合并策略需为 {sorted(ALLOWED_MERGE_STRATEGIES)} 之一")
        return value


class RuleResponse(BaseModel):
    rule_id: UUID
    checklist_type: str
    field_key: str
    target_key: str | None = None
    field_name_cn: str
    target_type: str = "fact"
    target_group_key: str | None = None
    target_group_name: str | None = None
    output_schema: dict[str, Any] = Field(default_factory=dict)
    source_policy: dict[str, Any] = Field(default_factory=dict)
    evidence_policy: dict[str, Any] = Field(default_factory=dict)
    field_description: str | None = None
    dimension_schema: dict[str, DimensionSpec] = Field(default_factory=dict)
    value_kind: str = "unknown"
    data_type: str
    unit: str | None = None
    required: bool = False
    multi_instance: bool = False
    merge_strategy: str = "value_consistent"
    strategy: dict[str, Any] = {}
    validation: dict[str, Any] | None = None
    fallback_value: str | None = None
    priority: int = 50
    enabled: bool = True
    version: int = 1
    created_at: datetime | None = None
    updated_at: datetime | None = None


class RuleTogglePayload(BaseModel):
    enabled: bool


class RuleImportPayload(BaseModel):
    """批量导入"""

    checklist_type: str
    rules: list[dict[str, Any]] = Field(default_factory=list, max_length=1000)


class ChangeLogResponse(BaseModel):
    log_id: UUID
    rule_id: UUID | None = None
    checklist_type: str | None = None
    action: str
    before_state: dict[str, Any] | None = None
    after_state: dict[str, Any] | None = None
    operator: str | None = None
    created_at: datetime | None = None
