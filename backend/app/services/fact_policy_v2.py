"""基于 P10 断言和内容失效记录的可靠提取读取策略。

这是持久化校验结果的投影，不是第二个模型核验器。
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from app.services.target_contract import target_type

VERSION = "reliable-extraction-v2"


def get(obj: Any, key: str, default=None):
    return obj.get(key, default) if isinstance(obj, Mapping) else getattr(obj, key, default)


def is_v2(run: Any = None, item: Mapping | None = None, detail: Mapping | None = None) -> bool:
    versions = [get(run, "contract_version"), (get(run, "extraction_profile", {}) or {}).get("contract_version"),
                (item or {}).get("contract_version"), (detail or {}).get("contract_version")]
    return any(value in {VERSION, 2, "2", "fact-contract-v2"} for value in versions)


def _values(selector: Mapping, *keys: str) -> set[str]:
    result: set[str] = set()
    for key in keys:
        values = selector.get(key)
        if values is not None:
            values = values if isinstance(values, list | tuple | set) else [values]
            result.update(str(value) for value in values)
    return result


def matching_invalidations(run: Any, fact: Any, invalidations=(), *, now: datetime | None = None) -> list[Any]:
    """选择器键之间为且关系；同一键组内的值为或关系。"""
    now = now or datetime.now(UTC).replace(tzinfo=None)
    matched = []
    provenance = get(fact, "provenance", {}) or {}
    refs = get(fact, "evidence", []) or []
    revision = provenance.get("content_revision") or provenance.get("revision_id") or get(run, "content_revision_id")
    groups = [
        (("document_id", "document_ids"), {str(get(run, "document_id"))}),
        (("revision_id", "revision_ids", "content_revision_id", "content_revision_ids"), {str(revision)}),
        (("parse_run_id", "parse_run_ids", "run_id", "run_ids"), {str(get(run, "parse_run_id"))}),
        (("field_key", "field_keys"), {str(get(fact, "field_key"))}),
        (("fact_id", "fact_ids"), {str(get(fact, "fact_id"))}),
        (("evidence_id", "evidence_ids"), {str(r.get("evidence_id")) for r in refs if isinstance(r, Mapping)}),
        (("pages", "page_numbers", "page_number"), {str(r.get("page_number", r.get("page_num"))) for r in refs if isinstance(r, Mapping)}),
        (("block_id", "block_ids"), {str(b) for r in refs if isinstance(r, Mapping) for b in
            ([r["block_id"]] if r.get("block_id") else r.get("segment_ids") or [])}),
        (("region_id", "region_ids"), {str(r.get("region_id")) for r in refs if isinstance(r, Mapping)}),
        (("generation_id", "generation_ids", "index_generation_id", "index_generation_ids"),
         {str(provenance.get("generation_id") or get(run, "index_generation_id"))}),
    ]
    supported = {key for keys, _ in groups for key in keys}
    for invalidation in invalidations:
        if str(get(invalidation, "document_id", get(run, "document_id"))) != str(get(run, "document_id")):
            continue
        effective = get(invalidation, "effective_at")
        if isinstance(effective, datetime):
            effective = effective.astimezone(UTC).replace(tzinfo=None) if effective.tzinfo else effective
            if effective > now:
                continue
        selector = get(invalidation, "scope_selector", {}) or {}
        # 未知选择器对本文档采用失败关闭，不能静默放行数据。
        unknown = set(selector) - supported - {"all", "reason", "scope", "request_id"}
        if unknown:
            matched.append(invalidation)
            continue
        if all(not _values(selector, *keys) or bool(_values(selector, *keys) & observed)
               for keys, observed in groups):
            matched.append(invalidation)
    return matched


def project_metadata(run: Any, field: Any, item: Mapping, rule: Mapping, fact: Any,
                     *, invalidations=()) -> dict:
    detail = get(field, "extraction_detail", {}) or {}
    completion = detail.get("field_completion") or detail.get("completion") or {}
    assertions = deepcopy(item.get("assertions") or item.get("attribute_support") or {})
    status = completion.get("semantic_state") or detail.get("semantic_state") or get(field, "validation_status")
    coverage = completion.get("coverage_complete", detail.get("coverage_complete", item.get("coverage_complete")))
    source = item.get("source_validity", "unknown")
    reasons = deepcopy(item.get("availability_reasons") or [])
    contract = rule.get("semantic_contract") or (rule.get("strategy") or {}).get("semantic_contract") or rule
    required = {"value", "unit", "value_kind", "source_role", "table_relation"}
    kind = target_type(rule)
    dimensions = contract.get("applicable_dimensions") or list(rule.get("dimension_schema") or {})
    required.update(f"dimensions.{key}" for key in dimensions)
    identity = contract.get("identity_dimensions") or ((rule.get("strategy") or {}).get("agent_extract") or {}).get("identity_dimensions") or list(rule.get("dimension_schema") or {})
    for key in identity:
        value = (item.get("dimensions") or {}).get(key)
        value = value.get("value") if isinstance(value, Mapping) else value
        if value in (None, "", "unknown", "未知"):
            reasons.append({"code": "IDENTITY_UNVERIFIED", "dimension": key})
    for key in sorted(required):
        assertion = assertions.get(key) or {}
        state = assertion.get("status")
        supported = state == "supported" and bool(assertion.get("basis") and assertion.get("evidence_ids"))
        if state == "not_applicable":
            supported = ((key == "unit" and contract.get("unit_policy", {}).get("mode") == "not_applicable")
                         or (key == "table_relation" and assertion.get("basis") == "source.non_table")
                         or (kind == "statement" and key in {"unit", "value_kind", "table_relation"}
                             and assertion.get("basis") == "target.statement"))
        if not supported:
            reasons.append({"code": "ATTRIBUTE_UNSUPPORTED", "attribute": key, "status": state or "unknown"})
    if coverage is not True:
        reasons.append({"code": "RANGE_SCOPE_OPEN"})
    if status != "completed":
        reasons.append({"code": "FIELD_SEARCH_NOT_COMPLETED", "status": status or "unknown"})
    if source != "valid":
        reasons.append({"code": "SOURCE_NOT_VALID", "status": source})
    revision = str(item.get("content_revision") or "")
    expected_revision = str(get(run, "content_revision_id") or "")
    if not revision or (expected_revision and revision != expected_revision):
        reasons.append({"code": "CONTENT_REVISION_UNVERIFIED"})
    if get(fact, "available_for_downstream") is not True:
        reasons.append({"code": "STRUCTURAL_VALIDATION_FAILED"})
    risks = [*get(fact, "risk", []), *(completion.get("risks") or [])]
    if kind == "table" and item.get("row_status") == "DELIVERABLE" and completion.get("range_closed") is True:
        # 同一目标里其他行待复核时，已绑定的行仍按本行交付。
        reasons = [reason for reason in reasons if reason.get("code") not in {
            "FIELD_SEARCH_NOT_COMPLETED", "RANGE_SCOPE_OPEN", "FACT_ATTRIBUTES_UNSUPPORTED",
        }]
        risks = [risk for risk in risks if not (isinstance(risk, Mapping) and risk.get("code") == "FACT_ATTRIBUTES_UNSUPPORTED")]
    blockers = {"SAME_IDENTITY_CONFLICT", "SAME_IDENTITY_VALUE_CONFLICT", "IDENTITY_UNVERIFIED",
                "OCR_CORRECTION_PENDING_PUBLICATION", "OCR_IMAGE_REQUIRED", "FACT_ATTRIBUTES_UNSUPPORTED",
                "CANDIDATES_UNRESOLVED", "CARDINALITY_CONFLICT"}
    for risk in risks:
        if isinstance(risk, Mapping) and risk.get("code") in blockers:
            reasons.append(deepcopy(dict(risk)))
    matches = matching_invalidations(run, fact, invalidations)
    invalidation_reason = None
    if matches:
        source = "invalid"
        invalidation_reason = "; ".join(str(get(i, "reason", "content_invalidated")) for i in matches)
        reasons.append({"code": "CONTENT_INVALIDATED", "invalidation_ids": [str(get(i, "invalidation_id")) for i in matches]})
    deduped = list({str(sorted(r.items())): r for r in reasons}.values())
    available = not deduped
    return {"contract_version": VERSION, "assertions": assertions,
            "automatic_quality_status": "PASSED" if available else "INCOMPLETE" if coverage is not True else "REVIEW_REQUIRED",
            "coverage_complete": coverage is True, "field_search_status": status or "unknown",
            "source_validity": source, "invalidation_reason": invalidation_reason,
            "availability_reasons": deduped, "human_review_status": "UNREVIEWED",
            "available_for_downstream": available}


async def load_invalidations(db: Any, document_id: Any) -> list[Any]:
    from sqlalchemy import select

    from app.models.content import ContentInvalidation

    result = await db.execute(select(ContentInvalidation).where(ContentInvalidation.document_id == document_id))
    return list(result.scalars().all())
