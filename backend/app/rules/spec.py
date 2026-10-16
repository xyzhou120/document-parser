"""规则的轻量视图对象

从 ORM 对象或字典构建，供提取策略消费，避免策略层依赖数据库会话。
"""

from copy import deepcopy
from dataclasses import asdict, dataclass, field
from typing import Any, cast

from app.rules.semantic_contract import normalize_rule_semantics, rule_semantic_hash


@dataclass
class RuleSpec:
    """一条提取规则的运行时视图"""

    field_key: str
    field_name_cn: str
    target_type: str = "fact"
    target_group_key: str | None = None
    target_group_name: str | None = None
    output_schema: dict = field(default_factory=dict)
    source_policy: dict = field(default_factory=dict)
    evidence_policy: dict = field(default_factory=dict)
    data_type: str = "string"
    unit: str | None = None
    required: bool = False
    multi_instance: bool = False
    merge_strategy: str = "value_consistent"
    strategy: dict = field(default_factory=dict)
    validation: dict | None = None
    fallback_value: str | None = None
    priority: int = 50
    rule_id: str | None = None
    checklist_type: str | None = None
    version: int = 1
    source: str = "database"
    dimensions: list[str] = field(default_factory=list)
    value_kind: str | None = None
    field_description: str | None = None
    dimension_schema: dict = field(default_factory=dict)

    def __post_init__(self):
        self.strategy = deepcopy(self.strategy)
        self.validation = deepcopy(self.validation)
        self.dimension_schema = deepcopy(self.dimension_schema)
        self.output_schema = deepcopy(self.output_schema)
        self.source_policy = deepcopy(self.source_policy)
        self.evidence_policy = deepcopy(self.evidence_policy)
        self.dimensions = list(self.dimensions)
        if self.dimension_schema:
            self.dimensions = list(self.dimension_schema)
        if "semantic_contract" in (self.strategy or {}):
            normalize_rule_semantics(self)

    @property
    def methods(self) -> list[str]:
        """提取方法降级链"""
        methods = (self.strategy or {}).get("methods") or []
        if not methods:
            # 兼容仅有 regex/keywords 的旧式配置
            if (self.strategy or {}).get("regex"):
                return ["regex"]
            return ["keyword_search"]
        return [m for m in methods if m]

    @property
    def semantic_contract(self) -> dict:
        """供执行和快照使用的独立 v2 语义契约。"""
        snapshot = normalize_rule_semantics(self)
        return {
            key: snapshot[key]
            for key in (
                "contract_version",
                "cardinality",
                "scope",
                "identity_dimensions",
                "applicable_dimensions",
                "value_kind_policy",
                "unit_policy",
                "retrieval",
                "evidence_requirements",
                "target_type",
                "target_group_key",
                "target_group_name",
                "output_schema",
                "source_policy",
                "evidence_policy",
            )
        }

    @property
    def cardinality(self) -> str:
        return cast(str, self.semantic_contract["cardinality"])

    @property
    def scope(self) -> dict:
        return cast(dict, self.semantic_contract["scope"])

    @property
    def identity_dimensions(self) -> list[str]:
        return cast(list[str], self.semantic_contract["identity_dimensions"])

    @property
    def applicable_dimensions(self) -> list[str]:
        return cast(list[str], self.semantic_contract["applicable_dimensions"])

    @property
    def rule_semantic_hash(self) -> str:
        return rule_semantic_hash(self)

    def semantic_snapshot(self) -> dict:
        """冻结原始运行时字段及规范化的 v2 身份。"""
        return {
            **asdict(self),
            **self.semantic_contract,
            "rule_version": self.version,
            "rule_semantic_hash": self.rule_semantic_hash,
            "adapted_from_contract_version": 2 if self.strategy.get("semantic_contract") else 1,
        }

    @property
    def confidence_threshold(self) -> float:
        value = (self.strategy or {}).get("confidence_threshold")
        if value is None:
            return 0.8
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.8

    @classmethod
    def from_orm(cls, rule: Any) -> "RuleSpec":
        """从 ExtractionRule ORM 对象构建"""
        return cls(
            field_key=rule.field_key,
            field_name_cn=rule.field_name_cn,
            target_type=getattr(rule, "target_type", None) or "fact",
            target_group_key=getattr(rule, "target_group_key", None),
            target_group_name=getattr(rule, "target_group_name", None),
            output_schema=dict(getattr(rule, "output_schema", None) or {}),
            source_policy=dict(getattr(rule, "source_policy", None) or {}),
            evidence_policy=dict(getattr(rule, "evidence_policy", None) or {}),
            data_type=rule.data_type or "string",
            unit=rule.unit,
            required=bool(rule.required),
            multi_instance=bool(rule.multi_instance),
            merge_strategy=rule.merge_strategy or "value_consistent",
            strategy=rule.strategy or {},
            validation=rule.validation,
            fallback_value=rule.fallback_value,
            priority=rule.priority or 50,
            rule_id=str(rule.rule_id),
            checklist_type=rule.checklist_type,
            version=rule.version or 1,
            source="database",
            dimensions=list((getattr(rule, "dimension_schema", None) or {}).keys())
            or list(getattr(rule, "dimensions", None) or []),
            value_kind=getattr(rule, "value_kind", None),
            field_description=getattr(rule, "field_description", None),
            dimension_schema=dict(getattr(rule, "dimension_schema", None) or {}),
        )

    @classmethod
    def from_config(cls, field: dict) -> "RuleSpec":
        """从 config/default.yaml 的 fields 列表构建（兜底种子）"""
        strategy = {
            "methods": field.get("extraction_methods") or ["keyword_search"],
            "keyword_search": {
                "keywords": field.get("keywords") or [],
                "regex": field.get("pattern"),
                "context_window": 50,
            },
        }
            # 配置文件可能已经在字段层携带 v2 契约。将其保留在 strategy 中，
            # 使数据库路径和种子路径共用同一种语义表示。
        contract = field.get("semantic_contract")
        if contract is None:
            contract = {
                key: field[key]
                for key in (
                    "contract_version",
                    "cardinality",
                    "scope",
                    "identity_dimensions",
                    "applicable_dimensions",
                    "value_kind_policy",
                    "unit_policy",
                    "retrieval",
                    "evidence_requirements",
                )
                if key in field
            }
        if contract:
            strategy["semantic_contract"] = contract
        return cls(
            field_key=field["field_key"],
            field_name_cn=field.get("field_name") or field["field_key"],
            target_type=field.get("target_type") or "fact",
            target_group_key=field.get("target_group_key"),
            target_group_name=field.get("target_group_name"),
            output_schema=dict(field.get("output_schema") or {}),
            source_policy=dict(field.get("source_policy") or {}),
            evidence_policy=dict(field.get("evidence_policy") or {}),
            data_type=_normalize_type(field.get("field_type")),
            unit=field.get("unit"),
            required=True,
            multi_instance=bool(field.get("multi_instance", False)),
            strategy=strategy,
            priority=50,
            checklist_type=field.get("checklist_type"),
            source="config",
            dimensions=list((field.get("dimension_schema") or {}).keys())
            or list(field.get("dimensions") or []),
            value_kind=field.get("value_kind"),
            field_description=field.get("field_description") or field.get("description"),
            dimension_schema=dict(field.get("dimension_schema") or {}),
        )


def _normalize_type(field_type: str | None) -> str:
    """把旧的 field_type 归一化为新的 data_type"""
    mapping = {
        "STRING": "string",
        "NUMBER": "float",
        "FLOAT": "float",
        "INTEGER": "integer",
        "DATE": "date",
        "ARRAY": "array",
        "JSON": "json",
    }
    if not field_type:
        return "string"
    return mapping.get(str(field_type).upper(), str(field_type).lower())
