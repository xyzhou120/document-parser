"""独立于持久化和提取执行的版本化规则语义。

新规则将契约持久化在 strategy.semantic_contract 中。规范化不会修改来源规则，
因此子运行可以安全冻结自己的快照。
"""

import hashlib
import json
import re
from copy import deepcopy
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class PageRange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: int = Field(ge=1)
    end: int = Field(ge=1)

    @model_validator(mode="after")
    def ordered(self):
        if self.end < self.start:
            raise ValueError("范围终页不能早于起页")
        return self


class RuleScope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["attachment", "sections", "explicit_ranges"] = "attachment"
    section_ids: list[str] = Field(default_factory=list)
    ranges: list[PageRange] = Field(default_factory=list)
    reason: str | None = None

    @model_validator(mode="after")
    def explicit_scope(self):
        if self.mode == "attachment":
            if self.section_ids or self.ranges:
                raise ValueError("全文范围不能携带章节或页码排除条件")
        elif not self.reason or not self.reason.strip():
            raise ValueError("限定范围必须记录业务原因")
        elif self.mode == "sections":
            if not self.section_ids or self.ranges or any(not x.strip() for x in self.section_ids):
                raise ValueError("章节范围需要非空 section_ids 且不能携带 ranges")
        elif not self.ranges or self.section_ids:
            raise ValueError("显式页码范围需要 ranges 且不能携带 section_ids")
        return self


class RetrievalPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    terms: list[str] = Field(default_factory=list)
    synonyms: list[str] = Field(default_factory=list)
    ocr_variants: list[str] = Field(default_factory=list)
    formula_patterns: list[str] = Field(default_factory=list)
    table_patterns: list[str] = Field(default_factory=list)
    # 旧版检索提示仍属于语义身份的一部分。
    labels: list[str] = Field(default_factory=list)
    exclude_terms: list[str] = Field(default_factory=list)
    units: list[str] = Field(default_factory=list)
    fallback_policy: Literal["complete_scope"] = "complete_scope"

    @field_validator(
        "terms",
        "synonyms",
        "ocr_variants",
        "labels",
        "exclude_terms",
        "units",
        "formula_patterns",
        "table_patterns",
    )
    @classmethod
    def nonblank(cls, values):
        if any(not x.strip() for x in values):
            raise ValueError("检索词项不能为空")
        return sorted(set(values))

    @field_validator("formula_patterns", "table_patterns")
    @classmethod
    def valid_patterns(cls, values):
        for pattern in values:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"检索正则无效: {exc}") from exc
        return values


def _default_kind_bases() -> list[Literal["wording", "header", "form_semantics"]]:
    return ["wording", "header", "form_semantics"]


class ValueKindPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["evidence_required"] = "evidence_required"
    allowed_bases: list[Literal["wording", "header", "form_semantics"]] = Field(
        default_factory=_default_kind_bases
    )
    unverified: Literal["unknown"] = "unknown"

    @field_validator("allowed_bases")
    @classmethod
    def supported_basis(cls, values):
        if not values:
            raise ValueError("值类型必须至少允许一种原文证据依据")
        return sorted(set(values))


class UnitPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["source_evidence", "not_applicable"] = "source_evidence"
    conversion_policy_ids: list[str] = Field(default_factory=list)
    preserve_raw: Literal[True] = True


class RuleSemanticContract(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contract_version: Literal[2] = 2
    cardinality: Literal["single", "multi_identity", "all_occurrences"] = "single"
    scope: RuleScope = Field(default_factory=RuleScope)
    identity_dimensions: list[str] = Field(default_factory=list)
    applicable_dimensions: list[str] = Field(default_factory=list)
    value_kind_policy: ValueKindPolicy = Field(default_factory=ValueKindPolicy)
    unit_policy: UnitPolicy = Field(default_factory=UnitPolicy)
    retrieval: RetrievalPolicy = Field(default_factory=RetrievalPolicy)
    evidence_requirements: list[str] = Field(default_factory=lambda: ["value"])

    @field_validator("identity_dimensions", "applicable_dimensions", "evidence_requirements")
    @classmethod
    def nonblank(cls, values):
        if any(not x.strip() for x in values):
            raise ValueError("语义合同列表不能为空字符串")
        return sorted(set(values))


def _get(rule: Any, key: str, default=None):
    return rule.get(key, default) if isinstance(rule, dict) else getattr(rule, key, default)


def normalize_rule_semantics(rule: Any) -> dict:
    """将 v1 规则适配到 v2，不虚构“全部出现项”的义务。

    返回独立的 JSON 兼容快照。身份之外的维度默认仍然适用；
    缺少维度值永远不等于 N/A。
    """
    strategy = deepcopy(_get(rule, "strategy") or {})
    knowledge = strategy.get("agent_extract") or {}
    dimensions = deepcopy(
        _get(rule, "dimension_schema") or {key: {} for key in (_get(rule, "dimensions") or [])}
    )
    explicit = deepcopy(strategy.get("semantic_contract") or {})
    legacy_retrieval = deepcopy(knowledge.get("retrieval") or {})
    synonyms = knowledge.get("synonyms") or []
    if isinstance(synonyms, str):
        synonyms = [synonyms]
    legacy_retrieval["synonyms"] = list(
        dict.fromkeys([*synonyms, *(legacy_retrieval.get("synonyms") or [])])
    )
    legacy_retrieval.setdefault("terms", [_get(rule, "field_name_cn") or _get(rule, "field_key")])
    identity = knowledge.get("identity_dimensions") or list(dimensions)
    defaults = {
        "cardinality": "multi_identity" if _get(rule, "multi_instance", False) else "single",
        "identity_dimensions": identity,
        "applicable_dimensions": list(dimensions),
        "retrieval": legacy_retrieval,
        "evidence_requirements": knowledge.get("evidence_requirements") or ["value"],
    }
    if not _get(rule, "unit"):
        defaults["unit_policy"] = {"mode": "not_applicable"}
    contract = RuleSemanticContract.model_validate({**defaults, **explicit}).model_dump()
    declared = set(dimensions)
    selected = set(contract["identity_dimensions"]) | set(contract["applicable_dimensions"])
    if selected - declared:
        raise ValueError("语义合同维度必须在 dimension_schema 或 dimensions 中声明")
    if set(contract["identity_dimensions"]) - set(contract["applicable_dimensions"]):
        raise ValueError("身份维度必须是适用维度")
    if _get(rule, "unit") and contract["unit_policy"]["mode"] == "not_applicable":
        raise ValueError("配置了期望单位的规则不能声明单位不适用")
    # 有意排除元数据：发布或重新排序未改变的规则不会改变其含义。
    # 所有影响提取的设置都会保留。
    return {
        "field_key": _get(rule, "field_key"),
        "field_name_cn": _get(rule, "field_name_cn"),
        "field_description": _get(rule, "field_description"),
        "data_type": _get(rule, "data_type", "string") or "string",
        "unit": _get(rule, "unit"),
        "value_kind": _get(rule, "value_kind") or "unknown",
        "required": bool(_get(rule, "required", False)),
        "merge_strategy": _get(rule, "merge_strategy", "value_consistent"),
        "dimension_schema": dimensions,
        "validation": deepcopy(_get(rule, "validation")),
        "fallback_value": _get(rule, "fallback_value"),
        "strategy": {key: value for key, value in strategy.items() if key != "semantic_contract"},
        **contract,
    }


def rule_semantic_hash(rule: Any) -> str:
    """对实际冻结语义计算摘要，不能只对 field_key 或版本计算摘要。"""
    payload = json.dumps(
        normalize_rule_semantics(rule),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
