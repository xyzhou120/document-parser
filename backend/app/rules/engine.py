"""规则驱动的提取引擎

按清单规则逐条提取字段：保留文档出现、归并同证据重复并标注提取质量。
"""

import logging
import math
from decimal import Decimal, InvalidOperation

from app.models.contracts import Box, CoordinateSystem, EvidenceRef, ExtractedField, Locator
from app.rules.spec import RuleSpec
from app.rules.strategies import Instance, get_strategy

logger = logging.getLogger(__name__)


class RuleEngine:
    """规则引擎"""

    async def extract_all(
        self, pages: list[dict], rules: list[RuleSpec]
    ) -> tuple[list[ExtractedField], list[dict]]:
        """执行全部规则

        Returns:
            (提取到的字段列表, 每个字段的实例明细)
        """
        ordered = sorted(rules, key=lambda r: r.priority, reverse=True)
        fields: list[ExtractedField] = []
        details: list[dict] = []

        for rule in ordered:
            try:
                field, detail = await self.extract_one(pages, rule)
            except Exception as exc:
                logger.error("规则 %s 执行失败: %s", rule.field_key, exc, exc_info=True)
                field, detail = None, {
                    "field_key": rule.field_key,
                    "field_name": rule.field_name_cn,
                    "required": rule.required,
                    "validation_status": "missing",
                    "rule_id": rule.rule_id,
                    "error": str(exc),
                    "instances": [],
                }
            if field:
                fields.append(field)
            details.append(detail)

        return fields, details

    async def extract_one(
        self, pages: list[dict], rule: RuleSpec
    ) -> tuple[ExtractedField | None, dict]:
        """执行单条规则"""
        instances: list[Instance] = []
        method_results: dict[str, list[Instance]] = {}

        executed: set[str] = set()
        for method in rule.methods:
            strategy = get_strategy(method)
            if strategy is None:
                logger.warning("未知提取方法 %s (字段 %s)", method, rule.field_key)
                continue
            if strategy.name in executed:
                continue
            executed.add(strategy.name)
            try:
                found = await strategy.run(rule, pages)
            except Exception as exc:
                logger.error("策略 %s 处理 %s 失败: %s", method, rule.field_key, exc)
                continue
            if found:
                method_results[strategy.name] = found
                instances.extend(found)

        detail: dict = {
            "field_key": rule.field_key,
            "field_name": rule.field_name_cn,
            "rule_id": rule.rule_id,
            "rule_version": rule.version,
            "data_type": rule.data_type,
            "unit": rule.unit,
            "required": rule.required,
            "multi_instance": rule.multi_instance,
            "merge_strategy": rule.merge_strategy,
            "extraction_method": "none",
            "instances": [i.to_dict() for i in instances],
            "validation_status": "passed",
        }

        if not instances:
            if rule.fallback_value is not None:
                detail["configured_fallback"] = rule.fallback_value
            detail["validation_status"] = "missing"
            return None, detail

        fused, fusion = self._fuse_candidates(rule, method_results)
        detail["fusion"] = fusion
        detail["rejected_candidates"] = fusion.get("rejected_candidates", [])
        if not fused:
            if rule.fallback_value is not None:
                detail["configured_fallback"] = rule.fallback_value
            detail["validation_status"] = "missing"
            return None, detail

        # 多处出现属于文档事实，平台不依据分数、页码或旧 merge_strategy 裁决主值。
        detail["instances"] = [item.to_dict() for item in fused]
        detail["instance_count"] = len(fused)
        keys = {self._candidate_key(rule, item.value) for item in fused}
        unique_value = len(keys) == 1
        representative = fused[0] if len(fused) == 1 else None
        statuses = [self._validate(rule, item)[0] for item in fused]
        for item_detail, status in zip(detail["instances"], statuses, strict=False):
            item_detail["quality_status"] = status
            item_detail["confidence_kind"] = "heuristic_uncalibrated"
            required_dimensions = [
                key for key, cfg in rule.dimension_schema.items() if cfg.get("required")
            ]
            item_detail["quality_issues"] = [
                {"code": "UNBOUND_DIMENSION", "dimension": key}
                for key in required_dimensions
                if key not in item_detail.get("dimensions", {})
            ]
            if rule.unit and not item_detail.get("unit"):
                item_detail["quality_issues"].append(
                    {"code": "UNIT_UNVERIFIED", "expected_unit": rule.unit}
                )
            elif rule.unit and item_detail.get("unit") != rule.unit:
                item_detail["quality_issues"].append(
                    {
                        "code": "UNIT_MISMATCH",
                        "expected_unit": rule.unit,
                        "observed_unit": item_detail["unit"],
                    }
                )
            if rule.value_kind and not item_detail.get("value_kind"):
                item_detail["quality_issues"].append(
                    {"code": "VALUE_KIND_UNVERIFIED", "expected_value_kind": rule.value_kind}
                )
        confidence = min(self._validate(rule, item)[1] for item in fused)
        final_method = "multipath" if len(method_results) > 1 else fused[0].source
        detail["extraction_method"] = final_method
        detail["validation_status"] = (
            "multiple_values"
            if not unique_value
            else (
                "multiple_occurrences"
                if len(fused) > 1
                else (
                    "review_required"
                    if any(status != "passed" for status in statuses)
                    or any(item["quality_issues"] for item in detail["instances"])
                    else "passed"
                )
            )
        )
        if (
            len(fused) == 1
            and detail["validation_status"] == "passed"
            and confidence < rule.confidence_threshold
        ):
            detail["validation_status"] = "low_confidence"
        detail["confidence"] = confidence
        detail["confidence_kind"] = "heuristic_uncalibrated"
        detail["merged_value"] = representative.value if representative else None
        return (
            self._build_field(
                rule,
                representative.value if representative else None,
                final_method,
                instance=representative,
                confidence=confidence,
            ),
            detail,
        )

    @staticmethod
    def _candidate_key(rule: RuleSpec, value: str | None) -> str:
        """数值格式归一化仅用于表示多处值相同，不删除实例。"""
        if (rule.data_type or "").lower() in ("float", "number", "integer", "int"):
            try:
                number = Decimal(str(value).replace(",", "").replace("，", "").strip())
                return f"number:{number.normalize()}"
            except (InvalidOperation, ValueError):
                pass
        return " ".join(str(value or "").strip().casefold().split())

    @staticmethod
    def _location_key(candidate: Instance) -> tuple | None:
        if candidate.start_offset is not None and candidate.end_offset is not None:
            return (candidate.page_num, "text", candidate.start_offset, candidate.end_offset)
        if candidate.table_id is not None and candidate.row_index is not None:
            return (
                candidate.page_num,
                "table",
                candidate.table_id,
                candidate.row_index,
                candidate.column_index,
            )
        if candidate.bbox:
            return (
                candidate.page_num,
                "box",
                candidate.coordinate_system,
                tuple(sorted(candidate.bbox.items())),
            )
        # 无位置证据时，不能假设两个相同引用就是同一次出现。
        return None

    def _fuse_candidates(
        self, rule: RuleSpec, method_results: dict[str, list[Instance]]
    ) -> tuple[list[Instance], dict]:
        """保留全部出现；仅同位置、同值的跨策略重复归并为同一个 occurrence。"""
        occurrences: list[Instance] = []
        locations: dict[tuple, Instance] = {}
        for method, candidates in method_results.items():
            for candidate in candidates:
                candidate.source = candidate.source or method
                candidate.methods = sorted(set(candidate.methods + [candidate.source]))
                location = self._location_key(candidate)
                key = (location, self._candidate_key(rule, candidate.value)) if location else None
                if key is not None and key in locations:
                    existing = locations[key]
                    existing.methods = sorted(set(existing.methods + candidate.methods))
                    # 重复检测不提高置信度，不改变原值/位置/维度。
                    continue
                occurrences.append(candidate)
                if key is not None:
                    locations[key] = candidate
        groups: dict[str, list[Instance]] = {}
        for candidate in occurrences:
            groups.setdefault(self._candidate_key(rule, candidate.value), []).append(candidate)
        return occurrences, {
            "status": "multiple_values" if len(groups) > 1 else ("agreed" if groups else "missing"),
            "candidate_groups": [
                {
                    "value": items[0].value,
                    "methods": sorted({method for item in items for method in item.methods}),
                    "pages": sorted({item.page_num for item in items if item.page_num > 0}),
                    "occurrence_ids": [item.occurrence_id for item in items],
                }
                for items in groups.values()
            ],
            "selected": None,
            "rejected_candidates": [],
        }

    # ---- 校验 ----

    def _validate(self, rule: RuleSpec, instance: Instance) -> tuple[str, float]:
        """执行 validation 配置，返回 (状态, 终值置信度)"""
        confidence = instance.confidence
        value = instance.value
        if value is None:
            return "invalid_format", confidence

        # 类型约束始终生效，即使规则没有额外 validation 配置。
        data_type = (rule.data_type or "string").lower()
        if data_type in ("float", "number", "integer", "int"):
            try:
                number = float(str(value).replace(",", "").replace("，", "").strip())
            except (TypeError, ValueError):
                return "not_a_number", max(0.4, confidence - 0.3)
            if not math.isfinite(number):
                return "not_a_number", max(0.4, confidence - 0.3)
            if data_type in ("integer", "int") and not number.is_integer():
                return "invalid_format", max(0.4, confidence - 0.25)

        validation = rule.validation or {}

        # 正则校验
        pattern = validation.get("regex")
        if pattern:
            import re

            try:
                if not re.fullmatch(pattern, value):
                    return "invalid_format", max(0.4, confidence - 0.25)
            except re.error:
                pass

        # 黑名单
        blacklist = validation.get("blacklist") or []
        if value in blacklist:
            return "blacklisted", max(0.3, confidence - 0.4)

        # 数值区间
        if data_type in ("float", "integer", "number", "int"):
            minimum = validation.get("min")
            maximum = validation.get("max")
            if minimum is not None and number < float(minimum):
                return "out_of_range", max(0.4, confidence - 0.25)
            if maximum is not None and number > float(maximum):
                return "out_of_range", max(0.4, confidence - 0.25)

            typical = validation.get("typical_range")
            if typical and len(typical) == 2:
                low, high = float(typical[0]), float(typical[1])
                if not (low <= number <= high):
                    # 超出典型范围不算错误，仅下调置信度
                    confidence = max(0.6, confidence - 0.1)
                    return "outlier", confidence

        return "passed", confidence

    # ---- 组装 ----

    def _build_field(
        self,
        rule: RuleSpec,
        value: str | None,
        method: str,
        instance: Instance | None = None,
        confidence: float | None = None,
    ) -> ExtractedField:
        """把实例转成 ExtractedField 契约对象

        仅使用解析器记录的真实 bbox；缺少 bbox 时保留偏移/引用且 locator 为空。
        """
        source = instance
        final_confidence = (
            confidence if confidence is not None else (source.confidence if source else 0.8)
        )

        evidence = None
        if source and source.page_num > 0 and source.context:
            locator = None
            if source.bbox:
                locator = Locator(
                    coordinate_system=CoordinateSystem(
                        source.coordinate_system or CoordinateSystem.PDF_POINT
                    ),
                    bbox=Box(**source.bbox),
                    page_number=source.page_num,
                )
            evidence = EvidenceRef(
                page_number=source.page_num,
                locator=locator,
                location_status=source.location_status,
                start_offset=source.start_offset,
                end_offset=source.end_offset,
                quote=source.context,
                confidence=max(0.0, min(1.0, final_confidence)),
            )

        return ExtractedField(
            field_key=rule.field_key,
            field_value=value,
            extraction_method=method,
            confidence=max(0.0, min(1.0, final_confidence)),
            evidence=evidence,
        )


rule_engine = RuleEngine()
