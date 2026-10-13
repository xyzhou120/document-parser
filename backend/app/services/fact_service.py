"""只读事实投影。分数不能替代证据、风险或人工来源信息。"""

import hashlib
import json
import math
from collections import defaultdict
from copy import deepcopy
from decimal import Decimal, InvalidOperation

from app.schemas.parse import FactResponse, FactViewResponse
from app.services.fact_policy_v2 import is_v2, matching_invalidations, project_metadata

CONTRACT_VERSION = "fact-contract-v1"
RELIABLE_CONTRACT_VERSION = "reliable-extraction-v2"


def _get(obj, key, default=None):
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()[:24]


def _present(value):
    return value is not None and str(value).strip() != ""


def _known(value):
    return _present(value) and str(value).lower() not in {"unknown", "未知"}


def _normalize(value, data_type):
    text = str(value).strip()
    if data_type not in {"int", "integer", "float", "number"}:
        return text, True
    try:
        number = Decimal(text.replace(",", "").replace("，", ""))
        if not number.is_finite() or (
            data_type in {"int", "integer"} and number != number.to_integral_value()
        ):
            return text, False
        return ("0" if number == 0 else format(number.normalize(), "f")), True
    except InvalidOperation:
        return text, False


def _unique(items):
    return list({_json(item): item for item in items}.values())


# 方案 C6（2026-10-10）：以下状态不能提供给下游。
# partial_candidates 表示搜索未完成；not_found 表示已完成搜索但没有候选；
# incomplete 表示预算、输出或页面可读性限制导致停止。每种状态都保留原因码供复核。
UNAVAILABLE_VALIDATION_STATUSES = {"partial_candidates", "not_found", "incomplete"}


def _risks(item):
    result = []
    for key in ("quality_issues", "issues", "risk"):
        for issue in item.get(key) or []:
            result.append(deepcopy(issue) if isinstance(issue, dict) else {"code": str(issue)})
    return result


def _evidence(item, registry):
    """解析注册表引用，并保留原生文本或 OCR 的页面、上下文证据，不伪造 bbox。"""
    result = deepcopy(item.get("evidence") or [])
    if isinstance(result, dict):
        result = [result]
    ids = list(item.get("evidence_ids") or [])
    roles = item.get("evidence_roles") or {}
    ids.extend(roles)
    for dimension in (item.get("dimensions") or {}).values():
        if isinstance(dimension, dict):
            ids.extend(dimension.get("evidence_ids") or [])
    for ref in item.get("evidence_refs") or []:
        if isinstance(ref, dict):
            result.append(deepcopy(ref))
        elif ref in registry:
            result.append({**deepcopy(registry[ref]), "evidence_id": ref})
    for ref in dict.fromkeys(ids):
        if ref in registry:
            result.append(
                {
                    **deepcopy(registry[ref]),
                    "evidence_id": ref,
                    "roles": roles.get(ref) or ["value"],
                }
            )
    if item.get("page_num") and (item.get("context") or item.get("quote") or item.get("bbox")):
        result.append({
            key: deepcopy(item[key])
            for key in ("page_num", "page_number", "bbox", "quote", "context",
                        "coordinate_system", "coordinate_origin", "page_rotation",
                        "page_width", "page_height", "location_status", "table_id",
                        "table_title", "row_index", "column_index", "start_offset",
                        "end_offset")
            if key in item and item[key] is not None
        })
        if result[-1].get("page_num") is not None:
            result[-1]["page_number"] = result[-1].pop("page_num")
        if result[-1].get("context") is not None:
            result[-1]["quote"] = result[-1].get("quote") or result[-1].pop("context")
    normalized = []
    for ref in result:
        if not isinstance(ref, dict):
            continue
        locator = ref.get("locator") or {}
        if not ref.get("bbox") and locator.get("bbox"):
            ref["bbox"] = deepcopy(locator["bbox"])
            ref["coordinate_system"] = locator.get("coordinate_system")
        normalized.append(ref)
    return _unique(normalized)


def _required_dimensions(rule):
    schema = rule.get("dimension_schema") or {}
    identity = ((rule.get("strategy") or {}).get("agent_extract") or {}).get("identity_dimensions")
    return list(
        dict.fromkeys(
            [
                *(identity or []),
                *(
                    key
                    for key, cfg in schema.items()
                    if isinstance(cfg, dict) and cfg.get("required")
                ),
            ]
        )
    )


def _fact(field, item, run, rule, index, *, reviewed=False):
    detail = _get(field, "extraction_detail") or {}
    value = str(item["value"])
    normalized, format_ok = _normalize(value, rule.get("data_type") or _get(field, "data_type"))
    dimensions = {
        key: deepcopy(val.get("value") if isinstance(val, dict) else val)
        for key, val in (item.get("dimensions") or {}).items()
    }
    required_dims = _required_dimensions(rule)
    dims_ok = all(_known(dimensions.get(key)) for key in required_dims)
    identity_dims = (
        ((rule.get("strategy") or {}).get("agent_extract") or {}).get("identity_dimensions")
        or list(rule.get("dimension_schema") or {})
        or rule.get("dimensions")
        or []
    )
    kind, role = item.get("value_kind") or "unknown", item.get("source_role") or "unknown"
    unit = item.get("unit") or None
    identity_known = (
        dims_ok
        and all(_known(dimensions.get(key)) for key in identity_dims)
        and all(_known(v) for v in dimensions.values())
        and _known(kind)
        and _known(role)
        and (not rule.get("unit") or bool(unit))
    )
    evidence = _evidence(item, detail.get("evidence_registry") or {})
    evidence_ok = any(
        isinstance(ref.get("page_number"), int)
        and not isinstance(ref["page_number"], bool)
        and ref["page_number"] > 0
        for ref in evidence
    )
    coverage = deepcopy(item.get("coverage") or detail.get("coverage") or {"status": "UNKNOWN"})
    # 方案 C6：按字段判断覆盖率。字段自身的失败计入；同一次运行其他字段的失败
    # 不会使当前字段变为 partial。
    field_failures = coverage["failures"] if "failures" in coverage else detail.get("failures")
    partial = bool(
        coverage.get("unvisited_pages")
        or coverage.get("unprocessed_pages")
        or coverage.get("status") in {"UNRESOLVED", "PARTIAL"}
        or field_failures
    )
    method = _get(field, "extraction_method")
    agent = method == "agent_extract" or item.get("source") == "agent_extract"
    score = item.get("confidence", detail.get("confidence", _get(field, "confidence")))
    if reviewed or agent or isinstance(score, bool) or not isinstance(score, int | float):
        score = None
    elif not math.isfinite(score) or not 0 <= score <= 1:
        score = None
    confidence_kind = (
        "human_assertion"
        if reviewed
        else (
            "uncalibrated_model"
            if agent
            else "heuristic_uncalibrated" if score is not None else "unknown"
        )
    )
    unit_ok = not rule.get("unit") or unit == rule["unit"]
    kind_ok = not rule.get("value_kind") or kind == rule["value_kind"]
    status = item.get("validation_status") or (
        None if reviewed else _get(field, "validation_status")
    )
    item_quality = item.get("quality_status")
    checks = {
        "value_format": format_ok,
        "evidence_present": evidence_ok,
        "required_dimensions_bound": dims_ok,
        "identity_known": identity_known,
        "unit_matches_rule": unit_ok,
        "value_kind_matches_rule": kind_ok,
        "coverage_complete": (
            False if partial else None if coverage.get("status") == "UNKNOWN" else True
        ),
        "validation_status": status,
    }
    risks = [*(_risks(detail) if not reviewed else []), *_risks(item)]
    if item_quality and item_quality not in {"passed", "REVIEW_REQUIRED"}:
        risks.append({"code": "INSTANCE_QUALITY_STATUS", "status": item_quality})
    for code, ok in (
        ("INVALID_VALUE_FORMAT", format_ok),
        ("EVIDENCE_MISSING", evidence_ok),
        ("UNBOUND_DIMENSION", dims_ok),
        ("IDENTITY_UNKNOWN", identity_known),
        ("UNIT_MISMATCH", unit_ok),
        ("VALUE_KIND_MISMATCH", kind_ok),
    ):
        if not ok:
            risks.append({"code": code})
    if partial:
        risks.append({"code": "PARTIAL_COVERAGE"})
    if status and status not in {"passed", "review_required"}:
        risks.append({"code": "VALIDATION_STATUS", "status": status})
    if status == "review_required" and item_quality and item_quality not in {"passed", "REVIEW_REQUIRED"}:
        risks.append({"code": "REVIEW_REQUIRED_QUALITY", "status": item_quality})
    legacy = item.get("_legacy", False)
    if legacy:
        risks.append({"code": "LEGACY_UNASSESSED"})
    identity = [dimensions, unit, kind, role]
    content_key = [_get(field, "field_key"), identity, normalized]
    if is_v2(run, item, detail):
        semantics = rule.get("semantic_contract") or (rule.get("strategy") or {}).get("semantic_contract") or rule
        if semantics.get("cardinality") == "all_occurrences":
            content_key += [item.get("occurrence_id") or item.get("candidate_id") or index]
    if not identity_known:
        content_key += [item.get("occurrence_id") or _digest(evidence), index]
    provenance = {
        "source": "human_review_revision" if reviewed else "automatic",
        "parse_run_id": str(run.parse_run_id),
        "document_id": str(run.document_id),
        "field_id": str(_get(field, "field_id")) if _get(field, "field_id") else None,
        "rule_id": str(_get(field, "rule_id")) if _get(field, "rule_id") else None,
        "rule_version": _get(field, "rule_version"),
        "extraction_method": method,
        "artifact_id": str(_get(run, "artifact_id")) if _get(run, "artifact_id") else None,
        "inherited_from_parse_run_id": detail.get("inherited_from_parse_run_id"),
        "occurrence_ids": item.get("occurrence_ids")
        or [item.get("occurrence_id") or f"item-{index}"],
    }
    if reviewed:
        provenance["automatic_risks"] = _risks(detail)
    raw_values = [
        str(v) for v in item.get("raw_values") or [item.get("raw_value", value)] if v is not None
    ]
    available = bool(
        format_ok
        and evidence_ok
        and dims_ok
        and unit_ok
        and kind_ok
        and not partial
        and status != "blocked"
        and status not in UNAVAILABLE_VALIDATION_STATUSES
        and (not agent or (_known(kind) and _known(role)))
    )
    result = FactResponse(
        fact_id="fact-" + _digest([str(run.document_id), str(run.parse_run_id), content_key]),
        field_key=_get(field, "field_key"),
        field_name=_get(field, "field_name", ""),
        raw=raw_values[0] if raw_values else value,
        raw_values=raw_values,
        normalized=normalized,
        value=value,
        unit=unit,
        dimensions=dimensions,
        value_kind=kind,
        source_role=role,
        evidence=evidence,
        confidence={"score": score, "kind": confidence_kind},
        checks=checks,
        risk=_unique(risks),
        coverage=coverage,
        provenance=provenance,
        available_for_downstream=None if legacy else available,
        review_status="HUMAN_REVIEWED" if reviewed else "UNREVIEWED",
    )
    if is_v2(run, item, detail):
        semantics = rule.get("semantic_contract") or (rule.get("strategy") or {}).get("semantic_contract") or rule
        result.provenance["content_revision"] = str(item.get("content_revision") or _get(run, "content_revision_id") or "")
        result.provenance["candidate_id"] = item.get("candidate_id")
        result.provenance["identity_dimensions"] = semantics.get("identity_dimensions") or identity_dims
        metadata = project_metadata(run, field, item, rule, result)
        for key, value in metadata.items():
            setattr(result, key, value)
        result.checks["identity_known"] = identity_known and all(
            (result.assertions.get(f"dimensions.{key}") or {}).get("status") == "supported"
            for key in identity_dims)
    return result


def _merge_facts(facts):
    merged = {}
    for fact in facts:
        existing = merged.get(fact.fact_id)
        if existing is None:
            merged[fact.fact_id] = fact
            continue
        existing.evidence = _unique([*existing.evidence, *fact.evidence])
        existing.raw_values = list(dict.fromkeys([*existing.raw_values, *fact.raw_values]))
        existing.risk = _unique([*existing.risk, *fact.risk])
        existing.provenance["occurrence_ids"] = list(
            dict.fromkeys(
                [
                    *existing.provenance["occurrence_ids"],
                    *fact.provenance["occurrence_ids"],
                ]
            )
        )
        scores = [existing.confidence.score, fact.confidence.score]
        existing.confidence.score = min(scores) if all(v is not None for v in scores) else None
        existing.available_for_downstream = (
            existing.available_for_downstream and fact.available_for_downstream
        )
    identities = defaultdict(list)
    for fact in merged.values():
        if fact.checks["identity_known"]:
            if fact.contract_version == RELIABLE_CONTRACT_VERSION:
                keys = fact.provenance.get("identity_dimensions") or []
                identities[_json([fact.field_key, {key: fact.dimensions.get(key) for key in keys}])].append(fact)
                continue
            identities[
                _json(
                    [fact.field_key, fact.dimensions, fact.unit, fact.value_kind, fact.source_role]
                )
            ].append(fact)
    for matches in identities.values():
        if len({fact.normalized for fact in matches}) > 1:
            for fact in matches:
                fact.risk.append(
                    {
                        "code": "SAME_IDENTITY_VALUE_CONFLICT",
                        "related_fact_ids": [other.fact_id for other in matches if other != fact],
                    }
                )
                if fact.contract_version == RELIABLE_CONTRACT_VERSION:
                    fact.available_for_downstream = False
                    fact.automatic_quality_status = "REVIEW_REQUIRED"
                    fact.availability_reasons.append({"code": "SAME_IDENTITY_VALUE_CONFLICT"})
    return sorted(merged.values(), key=lambda fact: (fact.field_key, fact.fact_id))


def build_fact_view(run, fields, *, view="automatic", revisions=(), invalidations=(), invalidation_resolver=None):
    """投影不可变的自动记录或显式人工复核修订。"""
    requested_view = view
    if view == "available":
        view = "automatic"
    profile = run.extraction_profile or {}
    rules = {rule["field_key"]: rule for rule in profile.get("rules", [])}
    field_map = {_get(field, "field_key"): field for field in fields}
    facts, reviews = [], []
    latest = {}
    for row in revisions:
        if row.field_key not in latest or row.revision > latest[row.field_key].revision:
            latest[row.field_key] = row
    for key, row in sorted(latest.items()):
        reviews.append(
            {
                "field_key": key,
                "review_id": str(row.review_id),
                "revision": row.revision,
                "action": row.action,
                "reviewer": row.reviewer,
                "reason": row.reason,
                "file_hash": row.file_hash,
                "created_at": str(row.created_at),
                "review_status": "REJECTED" if row.action == "REJECT" else "HUMAN_REVIEWED",
            }
        )
    reviews_by_key = {review["field_key"]: review for review in reviews}
    if view == "reviewed":
        for key, row in sorted(latest.items()):
            review = reviews_by_key[key]
            if row.action == "REJECT":
                continue
            field = field_map.get(key) or {"field_key": key, "field_name": key}
            for index, item in enumerate(row.facts or []):
                if not isinstance(item, dict) or not _present(item.get("value")):
                    continue
                fact = _fact(field, item, run, rules.get(key, {}), index, reviewed=True)
                fact.fact_id = "fact-" + _digest([fact.fact_id, str(row.review_id), row.revision])
                fact.provenance.update(review)
                # 修正是新的断言，不是之前的自动概率。
                fact.provenance["automatic_confidence"] = _get(field, "confidence")
                facts.append(fact)
    elif view == "automatic":
        for field in fields:
            detail = _get(field, "extraction_detail") or {}
            instances = _get(field, "instances") or detail.get("instances") or []
            groups = detail.get("value_groups") or []
            items = deepcopy(groups or instances)
            if (
                _get(field, "extraction_method") == "fallback"
                or _get(field, "validation_status") == "fallback_used"
            ):
                continue
            if not items and _present(_get(field, "field_value")):
                items = [
                    {
                        "value": _get(field, "field_value"),
                        "unit": _get(field, "unit"),
                        "evidence": _get(field, "evidence") or [],
                        "_legacy": True,
                    }
                ]
            for index, item in enumerate(items):
                if not isinstance(item, dict) or not _present(item.get("value")):
                    continue
                # 聚合投影保留所有出现项级别的校验和维度证据。
                if groups:
                    occurrence_ids = set(item.get("occurrence_ids") or [])
                    related = [
                        instance
                        for instance in instances
                        if instance.get("occurrence_id") in occurrence_ids
                    ]
                    item["issues"] = [
                        *_risks(item),
                        *(issue for instance in related for issue in _risks(instance)),
                    ]
                    item["evidence"] = [
                        *(item.get("evidence") or []),
                        *(
                            ref
                            for instance in related
                            for ref in _evidence(instance, detail.get("evidence_registry") or {})
                        ),
                    ]
                facts.append(
                    _fact(field, item, run, rules.get(_get(field, "field_key"), {}), index)
                )
    else:
        raise ValueError("Unsupported fact view")
    for fact in facts:
        fact.field_review_status = reviews_by_key.get(fact.field_key, {}).get(
            "review_status", "UNREVIEWED"
        )
        if view == "automatic" and fact.field_key in reviews_by_key:
            action = reviews_by_key[fact.field_key]["action"]
            if action in {"REJECT", "CORRECT"}:
                fact.risk.append(
                    {
                        "code": (
                            "HUMAN_REJECTED" if action == "REJECT" else "HUMAN_CORRECTION_AVAILABLE"
                        ),
                        "review_id": reviews_by_key[fact.field_key]["review_id"],
                    }
                )
    merged = _merge_facts(facts)
    for fact in merged:
        matches = matching_invalidations(run, fact, invalidations)
        if invalidation_resolver is not None:
            matches = [*matches, *(invalidation_resolver(run, fact) or [])]
        if matches:
            fact.source_validity = "invalid"
            fact.invalidation_reason = "; ".join(str(_get(item, "reason", "content_invalidated")) for item in matches)
            fact.available_for_downstream = False
            if fact.contract_version == RELIABLE_CONTRACT_VERSION:
                fact.automatic_quality_status = "REVIEW_REQUIRED"
            fact.availability_reasons.append({"code": "CONTENT_INVALIDATED", "reason": fact.invalidation_reason})
            fact.risk.append({"code": "CONTENT_INVALIDATED", "reason": fact.invalidation_reason})
        if view == "reviewed":
            fact.human_review_status = "HUMAN_REVIEWED"
    reliable = is_v2(run) or any(f.contract_version == RELIABLE_CONTRACT_VERSION for f in merged)
    if requested_view == "available":
        merged = [fact for fact in merged if fact.available_for_downstream is True]
    return FactViewResponse(
        contract_version=RELIABLE_CONTRACT_VERSION if reliable else CONTRACT_VERSION,
        schema_version="v2" if reliable else "v1",
        parse_run_id=run.parse_run_id,
        document_id=run.document_id,
        view=requested_view,
        facts=merged,
        reviews=reviews,
        missing_fields=profile.get("missing_fields") or [],
        warnings=profile.get("warnings") or [],
        availability_policy="complete_scope_v2" if reliable else "structural_v1",
    )


def compare_fact_views(before: FactViewResponse, after: FactViewResponse):
    """比较所有事实的语义，同时保留身份未知的重复出现项。"""
    if before.document_id != after.document_id:
        raise ValueError("Runs must belong to the same document")

    def content(fact):
        return _json(
            [
                fact.field_key,
                fact.normalized,
                fact.unit,
                fact.dimensions,
                fact.value_kind,
                fact.source_role,
            ]
        )

    old, new = defaultdict(list), defaultdict(list)
    for fact in before.facts:
        old[content(fact)].append(fact)
    for fact in after.facts:
        new[content(fact)].append(fact)
    added: list[dict] = []
    removed: list[dict] = []
    changed: list[dict] = []
    unchanged = 0
    for key in sorted(old.keys() | new.keys()):
        left, right = old[key], new[key]
        paired = min(len(left), len(right))
        for index in range(paired):
            a, b = left[index], right[index]

            def quality(fact):
                return _json(
                    [
                        fact.evidence,
                        fact.confidence.model_dump(),
                        fact.risk,
                        fact.coverage,
                        fact.review_status,
                        fact.checks,
                        fact.available_for_downstream,
                    ]
                )

            if quality(a) == quality(b):
                unchanged += 1
            else:
                changed.append(
                    {"before": a.model_dump(mode="json"), "after": b.model_dump(mode="json")}
                )
        removed.extend(f.model_dump(mode="json") for f in left[paired:])
        added.extend(f.model_dump(mode="json") for f in right[paired:])
    return {
        "contract_version": CONTRACT_VERSION,
        "document_id": str(before.document_id),
        "before_run_id": str(before.parse_run_id),
        "after_run_id": str(after.parse_run_id),
        "added": added,
        "removed": removed,
        "changed": changed,
        "unchanged_count": unchanged,
    }


def build_run_facts(run, fields, *, invalidations=()):
    """为只需要自动事实列表的消费者提供便捷投影。"""
    return build_fact_view(run, fields, invalidations=invalidations).facts


async def build_fact_view_with_invalidations(db, run, fields, **kwargs):
    """当前事实读取器：应用文档来源的全部有效失效记录。"""
    from app.services.fact_policy_v2 import load_invalidations

    return build_fact_view(run, fields, invalidations=await load_invalidations(db, run.document_id), **kwargs)
