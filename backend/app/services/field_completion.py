"""可靠提取 v2 的范围、候选、证据和身份闭合。"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from typing import Any

from app.services.candidate_retrieval import normalize, normalize_unit
from app.services.evidence_verification import (
    SUPPORTED,
    TERMINAL_CANDIDATE_STATES,
    get,
    semantic_contract,
    validate_candidate_decision,
)
from app.services.target_contract import target_type


def _range_closed(item: Any) -> bool:
    if get(item, "scan_done") is not True or get(item, "quality_blockers", None) is None:
        return False
    if get(item, "quality_blockers"):
        return False
    for key in ("state", "quality_state", "coverage_status", "source_validity"):
        if str(get(item, key, "")).lower() in {
            "failed", "failed_terminal", "unknown", "unreadable", "incomplete", "partial",
            "invalid", "superseded", "pending", "cancelled",
        }:
            return False
    return get(item, "coverage_complete", True) is True


def _cid(candidate: Any) -> str:
    return str(get(candidate, "candidate_id") or get(candidate, "id") or "")


def _state(candidate: Any) -> str:
    return str(get(candidate, "state") or "pending")


def _dimension(fact: Mapping, key: str) -> Any:
    item = (fact.get("dimensions") or {}).get(key)
    return item.get("value") if isinstance(item, Mapping) else item


def _identity(fact: Mapping, keys: Sequence[str]) -> tuple[str, ...]:
    return tuple(normalize(_dimension(fact, key)) for key in keys)


def _value_key(fact: Mapping) -> tuple[str, str, str]:
    value = str(fact.get("value"))
    try:
        value = str(Decimal(value).normalize())
    except (InvalidOperation, ValueError):
        value = normalize(value)
    return value, normalize_unit(fact.get("unit")), str(fact.get("value_kind") or "")


def _fact_supported(fact: Mapping, contract: Mapping, revision: str | None) -> bool:
    if fact.get("usable") is False or fact.get("row_status") == "REVIEW_REQUIRED":
        return False
    assertions = fact.get("assertions") or fact.get("attribute_support") or {}
    required = {"value", "unit", "value_kind", "source_role", "table_relation"}
    required.update(f"dimensions.{key}" for key in contract.get("applicable_dimensions", []))
    if fact.get("source_validity") != "valid" or not fact.get("content_revision"):
        return False
    if revision and str(fact["content_revision"]) != revision:
        return False
    for key in required:
        assertion = assertions.get(key)
        if not isinstance(assertion, Mapping) or assertion.get("status") not in SUPPORTED:
            return False
        if assertion.get("status") == "supported" and (not assertion.get("evidence_ids") or not assertion.get("basis")):
            return False
        if assertion.get("status") == "not_applicable":
            permitted = ((key == "unit" and contract.get("unit_policy", {}).get("mode") == "not_applicable")
                         or (key == "table_relation" and assertion.get("basis") == "source.non_table")
                         or (assertion.get("basis") == "target.statement" and key in {"unit", "value_kind", "table_relation"})
                         or (key.startswith("dimensions.") and assertion.get("basis") == "rule.dimension_optional")
                         or key in contract.get("not_applicable_attributes", []))
            if not permitted:
                return False
    return True


def _dimension_schema(rule: Any) -> Mapping:
    schema = get(rule, "dimension_schema") or {}
    return schema if isinstance(schema, Mapping) else {}


def _required_identity_keys(rule: Any, keys: Sequence[str]) -> list[str]:
    """只把规则标成必填的身份维度当作缺失即未绑定。

    没有 required 标记的旧规则仍要求全部身份键。评价单元等选填维度空着时，
    事实继续挂在工作面上，不把每一行都收成身份未核验。
    """
    schema = _dimension_schema(rule)
    declares_required = any(isinstance(cfg, Mapping) and "required" in cfg for cfg in schema.values())
    if not declares_required:
        return list(keys)
    return [key for key in keys if isinstance(schema.get(key), Mapping) and schema[key].get("required") is True]


def _merge_facts(facts: Sequence[Mapping], keys: Sequence[str], cardinality: str,
                 required_keys: Sequence[str] | None = None) -> list[dict]:
    merged: dict[tuple, dict] = {}
    required = list(keys if required_keys is None else required_keys)
    for source in facts:
        fact = deepcopy(dict(source))
        identity = _identity(fact, keys)
        # 必填身份缺失时保留为独立的诊断出现项。选填维度空着不拆组。
        key: tuple[Any, ...] = (identity, _value_key(fact))
        required_identity = _identity(fact, required)
        if any(not value or value == "unknown" for value in required_identity) or cardinality == "all_occurrences":
            key += (str(fact.get("occurrence_id") or fact.get("candidate_id") or len(merged)),)
        if key not in merged:
            fact["candidate_ids"] = [str(fact["candidate_id"])] if fact.get("candidate_id") else []
            fact["occurrence_ids"] = [str(fact["occurrence_id"])] if fact.get("occurrence_id") else []
            merged[key] = fact
        else:
            old = merged[key]
            old["evidence_ids"] = sorted(set(old.get("evidence_ids", [])) | set(fact.get("evidence_ids", [])))
            for attr in ("candidate_ids", "occurrence_ids"):
                new_key = "candidate_id" if attr == "candidate_ids" else "occurrence_id"
                if fact.get(new_key):
                    old[attr] = sorted(set(old[attr]) | {str(fact[new_key])})
    return list(merged.values())


def complete_field(rule: Any, ranges: Sequence[Any] | Mapping, candidates: Sequence[Any],
                   facts: Sequence[Mapping], *, applicability: Mapping | None = None,
                   candidate_decisions: Sequence[Mapping] | None = None,
                   evidence: Mapping | None = None) -> dict:
    """只闭合冻结的范围分母以及所有候选和对象工作。

    ranges 可以是冻结序列，也可以是包含 items、expected_keys、revision_id、
    rule_semantic_hash 和 scope_snapshot 的封装对象。预期键是策略 x 范围键。
    封装对象会检测缺失工作项，不会接受被缩短的列表。
    候选 rejected/duplicate 处置必须由调用方完成平台校验。
    """
    contract = semantic_contract(rule)
    envelope = dict(ranges) if isinstance(ranges, Mapping) else {}
    items = list(envelope.get("items") or []) if envelope else list(ranges)
    candidate_list = list(candidates)
    facts_list = [deepcopy(dict(f)) for f in facts]
    risks: list[dict] = []
    revision = str(envelope.get("revision_id") or "") or None
    if not revision:
        revisions = {str(get(item, "revision_id") or get(item, "content_revision")) for item in items
                     if get(item, "revision_id") or get(item, "content_revision")}
        if len(revisions) == 1:
            revision = next(iter(revisions))
        elif len(revisions) > 1:
            risks.append({"code": "RANGE_REVISION_MISMATCH"})
    if not revision:
        risks.append({"code": "RANGE_REVISION_UNVERIFIED"})
    if candidate_decisions:
        by_id = {_cid(c): c for c in candidate_list}
        for decision in candidate_decisions:
            candidate = by_id.get(str(decision.get("candidate_id") or ""))
            if candidate is None:
                risks.append({"code": "UNKNOWN_CANDIDATE_DECISION"})
                continue
            result = validate_candidate_decision(decision, candidate, rule, evidence)
            if result["fact"]:
                facts_list.append(result["fact"])
            risks.extend(result["issues"])
    range_keys = [f"{get(item, 'strategy_version', '')}:{get(item, 'range_key', '')}" for item in items]
    expected_keys = set(envelope.get("expected_keys") or range_keys)
    missing_ranges = expected_keys - set(range_keys)
    if missing_ranges:
        risks.append({"code": "RANGES_MISSING", "keys": sorted(missing_ranges)})
    if len(range_keys) != len(set(range_keys)):
        risks.append({"code": "RANGE_KEYS_DUPLICATE"})
    if any(not get(item, "range_key") or not get(item, "strategy_version") for item in items):
        risks.append({"code": "RANGE_KEYS_UNVERIFIED"})
    range_closed = bool(items) and all(_range_closed(item) for item in items) and not missing_ranges
    if not items:
        risks.append({"code": "RANGE_SCOPE_EMPTY"})
    elif not range_closed:
        risks.append({"code": "RANGE_SCOPE_OPEN"})
    counts = dict(Counter(_state(item) for item in candidate_list))
    pending = [c for c in candidate_list if _state(c) not in TERMINAL_CANDIDATE_STATES]
    unresolved = [c for c in candidate_list if _state(c) == "unresolved"]
    ids = [_cid(c) for c in candidate_list]
    if len(ids) != len(set(ids)) or any(not value for value in ids):
        risks.append({"code": "CANDIDATE_IDS_INVALID"})
    if pending:
        risks.append({"code": "CANDIDATES_UNFINISHED", "count": len(pending)})
    if unresolved:
        risks.append({"code": "CANDIDATES_UNRESOLVED", "count": len(unresolved)})
    accepted_ids = {_cid(c) for c in candidate_list if _state(c) == "accepted"}
    fact_ids = {str(f.get("candidate_id") or "") for f in facts_list}
    if accepted_ids - fact_ids:
        risks.append({"code": "ACCEPTED_CANDIDATE_WITHOUT_FACT", "ids": sorted(accepted_ids - fact_ids)})
    if fact_ids - set(ids):
        risks.append({"code": "FACT_WITHOUT_CANDIDATE", "ids": sorted(fact_ids - set(ids))})
    for candidate in candidate_list:
        if _state(candidate) == "duplicate":
            duplicate_of = get(candidate, "duplicate_of") or (get(candidate, "evidence") or {}).get("duplicate_of")
            if not duplicate_of or str(duplicate_of) not in fact_ids or str(duplicate_of) == _cid(candidate):
                risks.append({"code": "DUPLICATE_TARGET_UNVERIFIED", "candidate_id": _cid(candidate)})
    supported = [_fact_supported(f, contract, revision) for f in facts_list]
    if not all(supported):
        risks.append({"code": "FACT_ATTRIBUTES_UNSUPPORTED", "count": supported.count(False)})
    keys = list(contract.get("identity_dimensions") or [])
    required_keys = _required_identity_keys(rule, keys)
    unknown_ids = [f for f in facts_list if any(not v or v == "unknown" for v in _identity(f, required_keys))]
    if unknown_ids:
        risks.append({"code": "IDENTITY_UNVERIFIED", "count": len(unknown_ids)})
    kind = target_type(rule)
    cardinality = "all_occurrences" if kind in {"statement", "table"} else contract.get("cardinality", "single")
    merged = _merge_facts(facts_list, keys, cardinality, required_keys)
    grouped: dict[tuple, list[dict]] = {}
    for fact in merged:
        grouped.setdefault(_identity(fact, keys), []).append(fact)
    conflicts = [values for identity, values in grouped.items() if kind == "fact" and not any(not v for v in identity)
                 and len({_value_key(v) for v in values}) > 1]
    if conflicts:
        risks.append({"code": "SAME_IDENTITY_CONFLICT", "count": len(conflicts)})
    if cardinality == "single" and len(merged) > 1:
        risks.append({"code": "CARDINALITY_CONFLICT", "count": len(merged)})
    if cardinality == "all_occurrences":
        locations = [str(f.get("occurrence_id") or "") for f in facts_list]
        if any(not location for location in locations):
            risks.append({"code": "OCCURRENCE_LOCATION_UNVERIFIED"})
        if not range_closed:
            risks.append({"code": "OCCURRENCE_SCOPE_OPEN"})
    # 适用性属于冻结规则语义。调用方提供的 “N/A” 字符串不能掩盖缺失字段，
    # 只有带有绑定证据的规则适用性声明才可以。
    if applicability and applicability.get("status") == "not_applicable":
        if not contract.get("applicability") or not applicability.get("evidence_ids") or not applicability.get("verified"):
            risks.append({"code": "NOT_APPLICABLE_UNPROVEN"})
        elif facts_list or unresolved:
            risks.append({"code": "NOT_APPLICABLE_CONFLICT"})
    execution_closed = range_closed and not pending and not unresolved
    row_review = kind == "table" and any(
        fact.get("usable") is False or fact.get("row_status") == "REVIEW_REQUIRED" for fact in facts_list
    )
    if kind == "table" and row_review:
        # 某一行待复核不代表整次检索没有闭合，也不把该诊断复制成字段级失败。
        risks = [risk for risk in risks if risk.get("code") != "FACT_ATTRIBUTES_UNSUPPORTED"]
    if facts_list:
        state = "partial_candidates" if not execution_closed else ("review_required" if risks or row_review else "completed")
    else:
        state = "not_found" if execution_closed and not risks else "incomplete"
    proof = None
    if state == "not_found":
        scopes = [{"strategy_version": get(r, "strategy_version"), "range_key": get(r, "range_key"),
                   "cursor": get(r, "cursor"), "scan_done": True,
                   "quality_blockers": [], "coverage_status": get(r, "coverage_status", "complete")}
                  for r in items]
        scope_snapshot = envelope.get("scope_snapshot") or contract.get("scope") or {}
        proof = {"revision_id": revision, "rule_semantic_hash": envelope.get("rule_semantic_hash") or get(rule, "rule_semantic_hash"),
                 "scope_snapshot": scope_snapshot, "range_count": len(expected_keys), "closed_ranges": len(items),
                 "ranges": scopes, "candidate_count": len(candidate_list), "candidate_states": counts,
                 "candidate_dispositions": [{"candidate_id": _cid(c), "state": _state(c)} for c in candidate_list],
                 "failures": [], "source_validity": "valid", "valid": True}
        proof["scope_digest"] = hashlib.sha256(json.dumps(proof, sort_keys=True, default=str).encode()).hexdigest()
    search_closed = state in {"completed", "not_found"} or (kind == "table" and execution_closed and bool(facts_list))
    return {"semantic_state": state, "facts": merged, "risks": risks,
            "absence_search_proof": proof, "coverage_complete": search_closed,
            "range_closed": range_closed, "candidate_counts": counts,
            "candidate_disposition_complete": not pending and not unresolved}


def value_groups(facts: Sequence[Mapping], identity_keys: Sequence[str]) -> list[dict]:
    """把已闭合事实投影成结果页使用的值组，保留非空身份。"""
    groups = []
    for fact in facts:
        if not isinstance(fact, Mapping):
            continue
        dims = fact.get("dimensions") or {}
        identity = {}
        for key in identity_keys:
            raw = dims.get(key)
            value = raw.get("value") if isinstance(raw, Mapping) else raw
            if value not in (None, "", "unknown"):
                identity[key] = value
        evidence = []
        if fact.get("page_num") or fact.get("bbox") or fact.get("quote"):
            evidence.append({
                "page_number": fact.get("page_num"),
                "bbox": fact.get("bbox"),
                "coordinate_system": fact.get("coordinate_system"),
                "coordinate_origin": fact.get("coordinate_origin"),
                "quote": fact.get("quote"),
                "location_status": "region" if fact.get("bbox") else "page",
            })
        groups.append({
            "value": fact.get("value"),
            "unit": fact.get("unit"),
            "identity": identity,
            "dimensions": identity,
            "evidence_ids": list(fact.get("evidence_ids") or []),
            "evidence_refs": evidence,
            "occurrence_ids": [fact["occurrence_id"]] if fact.get("occurrence_id") else [],
            "raw_values": [fact.get("value")] if fact.get("value") is not None else [],
        })
    return groups
