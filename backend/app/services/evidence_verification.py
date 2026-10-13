"""Pure attribute evidence checks for reliable extraction v2.

The evidence argument is a platform-owned registry, never the model's receipt objects. A
receipt identifies its source, immutable revision and candidate/block binding. Original-image
receipts additionally carry attribute verification; attaching an image is not verification.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from app.services.candidate_retrieval import (
    NUMBER_RE,
    NUMERIC_TYPES,
    find_units,
    normalize,
    normalize_unit,
)
from app.services.candidate_retrieval import (
    value_in_text as _search_value_in_text,
)

TERMINAL_CANDIDATE_STATES = frozenset({"accepted", "rejected", "duplicate", "unresolved"})
SUPPORTED = frozenset({"supported", "not_applicable"})
KIND_TERMS = {
    "calculated": ("计算", "推算", "calculated", "computed"),
    "measured": ("实测", "测定", "测量", "检测", "measured"),
    "design": ("设计", "design"),
    "actual": ("实际", "施工完成", "实际施工", "actual"),
    "threshold": ("限值", "阈值", "临界", "不得超过", "标准值", "threshold"),
    "reported": ("报告值", "reported"),
    "original": ("原始", "original"),
}


def _evidence_text(text: Any) -> str:
    """Keep table-cell separators while retaining existing unit/text normalization."""
    return " ".join(normalize(part) for part in re.split(r"\s+", str(text or "")))


def value_in_text(value: Any, text: str, numeric: bool) -> bool:
    if not numeric:
        return bool(_search_value_in_text(value, text, False))
    if value is None:
        return False
    try:
        wanted = Decimal(normalize(value).replace(",", ""))
    except (InvalidOperation, ValueError):
        return False
    if not wanted.is_finite():
        return False
    return any(Decimal(match.group()) == wanted for match in NUMBER_RE.finditer(_evidence_text(text)))


def get(value: Any, key: str, default: Any = None) -> Any:
    return value.get(key, default) if isinstance(value, Mapping) else getattr(value, key, default)


def semantic_contract(rule: Any) -> dict:
    if isinstance(rule, Mapping) and "cardinality" in rule:
        return dict(rule)
    direct = get(rule, "semantic_contract")
    if isinstance(direct, Mapping):
        return dict(direct)
    from app.rules.semantic_contract import normalize_rule_semantics

    return dict(normalize_rule_semantics(rule))


def _input(candidate: Any) -> dict:
    payload = get(candidate, "payload")
    if isinstance(payload, Mapping):
        return {**payload, "candidate_id": get(candidate, "candidate_id"),
                "source": get(candidate, "source", payload.get("source")),
                "image_refs": list(get(candidate, "image_refs", ()))}
    if isinstance(candidate, Mapping):
        return dict(candidate)
    return {key: get(candidate, key) for key in (
        "candidate_id", "text", "window", "context", "source", "page_number", "bbox", "table",
        "stable_key", "segment_ids", "revision_id", "content_revision",
    )}


def _attribute(decision: Mapping, key: str) -> tuple[Any, dict]:
    attributes = decision.get("attributes") or {}
    proposal = attributes.get(key) if isinstance(attributes, Mapping) else None
    if key.startswith("dimensions."):
        raw = (decision.get("dimensions") or {}).get(key.split(".", 1)[1])
    else:
        raw = decision.get(key)
    if isinstance(raw, Mapping) and "value" in raw:
        proposal = {**raw, **(proposal or {})}
        raw = raw.get("value")
    return raw, dict(proposal) if isinstance(proposal, Mapping) else {}


def _ids(decision: Mapping, proposal: Mapping) -> list[str]:
    ids = proposal.get("evidence_ids")
    if ids is None:
        ids = decision.get("attribute_evidence", {}).get(proposal.get("attribute", ""))
    if ids is None:
        ids = decision.get("evidence_ids") or []
    if isinstance(ids, str):
        ids = [ids]
    return list(dict.fromkeys(str(v) for v in ids if isinstance(v, str | int))) if isinstance(ids, list | tuple) else []


def _receipt_attribute(receipt: Mapping, key: str) -> dict:
    verified = receipt.get("verification") or {}
    attributes = verified.get("attributes") or receipt.get("attributes") or {}
    item = attributes.get(key) if isinstance(attributes, Mapping) else None
    if item is None and key.startswith("dimensions."):
        dimensions = attributes.get("dimensions") if isinstance(attributes, Mapping) else None
        item = dimensions.get(key.split(".", 1)[1]) if isinstance(dimensions, Mapping) else None
    return dict(item) if isinstance(item, Mapping) else {}


def _same_value(left: Any, right: Any, key: str, numeric: bool) -> bool:
    if key == "unit":
        return bool(left) and normalize_unit(left) == normalize_unit(right)
    if key == "value" and numeric:
        return bool(value_in_text(left, str(right), True))
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        return json.dumps(left, sort_keys=True, default=str) == json.dumps(right, sort_keys=True, default=str)
    return left not in (None, "") and normalize(left) == normalize(right)


def _bound_receipts(ids: list[str], registry: Mapping, candidate: dict) -> tuple[list[tuple[str, dict]], list[str]]:
    good, errors = [], []
    cid = str(candidate.get("candidate_id") or candidate.get("id") or "")
    revision = str(candidate.get("content_revision") or candidate.get("revision_id") or "")
    segments = set(candidate.get("segment_ids") or [])
    for eid in ids:
        receipt = registry.get(eid)
        if not isinstance(receipt, Mapping):
            errors.append("EVIDENCE_MISSING")
            continue
        source = receipt.get("source") or receipt.get("source_kind")
        if source is None:
            errors.append("EVIDENCE_SOURCE_UNDECLARED")
            if candidate.get("source") == "ocr":
                errors.append("OCR_IMAGE_REQUIRED")
            if not receipt.get("content_revision") and not receipt.get("revision_id"):
                errors.append("EVIDENCE_REVISION_UNDECLARED")
            continue
        if source not in {"native_text", "text", "ocr_text", "original_image", "page_image", "region_image"}:
            errors.append("EVIDENCE_SOURCE_UNDECLARED")
            continue
        ev_revision = str(receipt.get("content_revision") or receipt.get("revision_id") or "")
        if not ev_revision:
            errors.append("EVIDENCE_REVISION_UNDECLARED")
            if candidate.get("source") == "ocr":
                errors.append("OCR_IMAGE_REQUIRED")
            continue
        if not revision or ev_revision != revision:
            errors.append("EVIDENCE_REVISION_MISMATCH")
            continue
        bound = str(receipt.get("candidate_id") or "") == cid and bool(cid)
        bound = bound or bool(segments.intersection(receipt.get("segment_ids") or []))
        if not bound:
            errors.append("EVIDENCE_CANDIDATE_MISMATCH")
            continue
        if receipt.get("invalidated") or receipt.get("source_validity") in {"invalid", "superseded", "unknown"}:
            errors.append("EVIDENCE_SOURCE_INVALID")
            continue
        good.append((eid, dict(receipt)))
    return good, errors


def _assert(observed: Any, expected: Any, status: str, evidence_ids=(), basis=None) -> dict:
    return {"status": status, "observed": observed, "expected": expected,
            "evidence_ids": list(evidence_ids), "basis": basis}


def _kind_wording_supported(kind: str, quote: str, value: Any, numeric: bool) -> bool:
    """Use the nearest explicit type marker for the target value, not any marker in the row."""
    for clause in re.split(r"[。；;，,\n]", quote):
        normalized = _evidence_text(clause).lower()
        if not value_in_text(value, clause, numeric):
            continue
        spans = [match.span() for match in NUMBER_RE.finditer(normalized)
                 if value_in_text(value, match.group(), numeric)] if numeric else [
                     (normalized.find(normalize(value).lower()), normalized.find(normalize(value).lower()) + len(normalize(value)))]
        for start, end in spans:
            matches = []
            for observed_kind, terms in KIND_TERMS.items():
                for term in terms:
                    for marker in re.finditer(re.escape(normalize(term).lower()), normalized):
                        distance = max(start - marker.end(), marker.start() - end, 0)
                        matches.append((distance, observed_kind))
            if matches:
                closest = min(distance for distance, _ in matches)
                nearest = {observed_kind for distance, observed_kind in matches if distance == closest}
                if nearest == {kind} and closest <= 32:
                    return True
    return False


def _unit_literal_supported(unit: str, text: str, value: Any, numeric: bool) -> bool:
    normalized = _evidence_text(text)
    if not numeric:
        return normalize_unit(unit) in [normalize_unit(v) for v in find_units(normalized, (unit,))]
    numbers = list(NUMBER_RE.finditer(normalized))
    for index, match in enumerate(numbers):
        if not value_in_text(value, match.group(), True):
            continue
        stop = numbers[index + 1].start() if index + 1 < len(numbers) else len(normalized)
        following = normalized[match.end():min(stop, match.end() + 16)]
        found = find_units(following, (unit,))
        if found and normalize_unit(found[0]) == normalize_unit(unit):
            return True
    return False


def _verify_attribute(key: str, observed: Any, expected: Any, decision: Mapping,
                      candidate: dict, registry: Mapping, contract: dict, numeric: bool) -> tuple[dict, list[str]]:
    _value, proposal = _attribute(decision, key)
    proposal["attribute"] = key
    ids = _ids(decision, proposal)
    receipts, errors = _bound_receipts(ids, registry, candidate)
    if observed in (None, "", "unknown"):
        return _assert(observed, expected, "unknown"), [*errors, f"{key.upper()}_UNVERIFIED"]
    source = candidate.get("source") or "native"
    text = str(candidate.get("text") or candidate.get("window") or "")
    context = str(candidate.get("context") or "")
    supported, bases = [], []
    for eid, receipt in receipts:
        receipt_source = receipt.get("source") or receipt.get("source_kind")
        image = receipt_source in {"original_image", "page_image", "region_image"}
        verified = _receipt_attribute(receipt, key)
        platform_basis = None
        if source == "ocr" and not image:
            continue
        if image:
            verification = receipt.get("verification") or {}
            if verification.get("status") != "verified" or verified.get("status") != "supported":
                continue
            if not _same_value(observed, verified.get("value"), key, numeric):
                continue
            if not verified.get("basis"):
                continue
            image_refs = list(candidate.get("image_refs") or [])
            ref = receipt.get("image_ref")
            image_key = (ref.get("image_hash") or ref.get("url") or ref.get("uri")) if isinstance(ref, Mapping) else ref
            candidate_ref = candidate.get("image_ref")
            candidate_image_key = (candidate_ref.get("image_hash") or candidate_ref.get("url") or candidate_ref.get("uri")) if isinstance(candidate_ref, Mapping) else candidate_ref
            if (not image_key or (image_refs and ref not in image_refs and image_key not in image_refs)
                    or (candidate_image_key and candidate_image_key != image_key)
                    or (candidate.get("region_id") and receipt.get("region_id") != candidate["region_id"])):
                errors.append("OCR_IMAGE_LINK_MISMATCH")
                continue
            if receipt.get("requires_publication"):
                errors.append("OCR_CORRECTION_PENDING_PUBLICATION")
                continue
            if key == "value" and not value_in_text(observed, text, numeric):
                if (not receipt.get("published") or receipt.get("transcription_revision") !=
                        (candidate.get("content_revision") or candidate.get("revision_id"))):
                    errors.append("OCR_CORRECTION_PENDING_PUBLICATION")
                    continue
            if key == "value_kind" and verified.get("basis") not in (
                    contract.get("value_kind_policy", {}).get("allowed_bases") or ["wording", "header", "form_semantics"]):
                continue
            supported.append(eid)
            bases.append(verified["basis"])
            continue
        ev_text = str(receipt.get("text") or receipt.get("quote") or "")
        scope_text = ev_text if receipt.get("scope") in {"context", "header"} else text
        if key == "value":
            valid = value_in_text(observed, text, numeric) and value_in_text(observed, ev_text, numeric)
        elif key == "unit":
            valid = _unit_literal_supported(str(observed), ev_text, decision.get("value"), numeric)
            if receipt.get("scope") == "header":
                valid = (verified.get("status") == "supported" and verified.get("basis") == "table_header_binding"
                         and _same_value(observed, verified.get("value"), key, numeric))
        elif key == "value_kind":
            quote = str(proposal.get("quote") or decision.get("quote") or ev_text)
            basis = proposal.get("basis") or "wording"
            allowed = contract.get("value_kind_policy", {}).get("allowed_bases") or ["wording", "header", "form_semantics"]
            marker = _kind_wording_supported(str(observed), quote, decision.get("value"), numeric)
            valid = (basis in allowed and basis in {"wording", "header"} and marker
                     and normalize(quote) in normalize(ev_text)
                     and (receipt.get("scope") == "header" or value_in_text(decision.get("value"), quote, numeric)))
            if verified.get("status") == "supported" and verified.get("basis") == "form_semantics":
                valid = "form_semantics" in allowed and _same_value(observed, verified.get("value"), key, numeric)
            if receipt.get("scope") == "header" and verified.get("basis") == "header":
                valid = ("header" in allowed and verified.get("status") == "supported"
                         and _same_value(observed, verified.get("value"), key, numeric))
            if (expected == observed == "reported" and
                    value_in_text(decision.get("value"), text, numeric) and
                    value_in_text(decision.get("value"), ev_text, numeric) and
                    value_in_text(decision.get("value"), quote, numeric) and
                    normalize(quote) in normalize(ev_text) and normalize(quote) in normalize(text)):
                # "Reported" is the generic literal stated by the document. It
                # does not assert measured/calculated/design semantics. OCR has
                # already been excluded from this text-only branch above.
                valid, platform_basis = True, "source.reported_literal"
        elif key.startswith("dimensions."):
            valid = value_in_text(observed, scope_text, False) and (
                value_in_text(observed, text, False) or value_in_text(observed, context, False))
        else:
            valid = bool(verified.get("status") == "supported" and verified.get("basis")
                         and _same_value(observed, verified.get("value"), key, numeric))
        if valid:
            supported.append(eid)
            bases.append(str(platform_basis or verified.get("basis") or proposal.get("basis") or "literal"))
    status = "supported" if supported else "unknown"
    if status == "supported" and expected not in (None, "", "unknown") and not _same_value(observed, expected, key, numeric):
        status = "mismatch"
        errors.append(f"{key.upper()}_MISMATCH")
    if not supported:
        errors.append(f"{key.upper()}_UNVERIFIED")
    return _assert(observed, expected, status, supported, sorted(set(bases))), errors


def validate_candidate_decision(decision: Mapping, candidate: Any, rule: Any,
                                evidence: Mapping | None = None) -> dict:
    """Return attribute assertions and a diagnostic fact; accepted means fully supported."""
    payload = _input(candidate)
    contract = semantic_contract(rule)
    cid = str(payload.get("candidate_id") or payload.get("id") or "")
    if str(decision.get("candidate_id") or decision.get("id") or "") != cid or not cid:
        return {"accepted": False, "usable": False, "fact": None, "assertions": {},
                "issues": [{"code": "CANDIDATE_ID_MISMATCH"}]}
    judgement = decision.get("judgement")
    if judgement in {"rejected", "duplicate"}:
        return {"accepted": True, "usable": False, "fact": None, "assertions": {}, "issues": [],
                "candidate_state": judgement}
    if judgement != "accepted":
        return {"accepted": False, "usable": False, "fact": None, "assertions": {},
                "issues": [{"code": "JUDGEMENT_INVALID"}]}
    numeric = str(get(rule, "data_type", contract.get("data_type", "string"))).lower() in NUMERIC_TYPES
    assertions, codes = {}, []
    expected = {"value": None, "unit": get(rule, "unit"), "value_kind": get(rule, "value_kind"),
                "source_role": None, "table_relation": None}
    dimensions = list(contract.get("applicable_dimensions") or [])
    expected.update({f"dimensions.{key}": None for key in dimensions})
    knowledge = (get(rule, "strategy", {}) or {}).get("agent_extract") or {}
    source_roles = knowledge.get("source_roles") or []
    if isinstance(source_roles, str):
        source_roles = [source_roles]
    allowed_roles = {str(role) for role in source_roles if isinstance(role, str) and role.strip()}
    for key, target in expected.items():
        observed, _ = _attribute(decision, key)
        if key == "unit" and contract.get("unit_policy", {}).get("mode") == "not_applicable":
            assertions[key] = _assert(observed, None, "not_applicable", basis="rule.unit_policy")
            continue
        if key == "table_relation" and payload.get("table") is False:
            assertions[key] = _assert(observed, None, "not_applicable", basis="source.non_table")
            continue
        assertions[key], errors = _verify_attribute(key, observed, target, decision, payload,
                                                    evidence or {}, contract, numeric)
        if key == "source_role" and allowed_roles and observed not in allowed_roles:
            assertions[key]["status"] = "mismatch" if observed not in (None, "", "unknown") else "unknown"
            assertions[key]["expected"] = sorted(allowed_roles)
            errors.append("SOURCE_ROLE_UNCONFIGURED")
        codes.extend(errors)
    usable = all(a["status"] in SUPPORTED for a in assertions.values())
    fact = {key: _attribute(decision, key)[0] for key in ("value", "unit", "value_kind", "source_role", "table_relation")}
    fact.update({"candidate_id": cid, "field_key": get(rule, "field_key"),
        "dimensions": {key: _attribute(decision, f"dimensions.{key}")[0] for key in dimensions},
        "content_revision": payload.get("content_revision") or payload.get("revision_id"),
        "assertions": assertions, "attribute_support": assertions, "usable": usable,
        "source_validity": "valid", "evidence_ids": sorted({eid for a in assertions.values() for eid in a["evidence_ids"]}),
        "page_num": payload.get("page_number") or payload.get("page"), "bbox": payload.get("bbox"),
        "segment_ids": list(payload.get("segment_ids") or []), "source": payload.get("source"),
        "quote": payload.get("text") or payload.get("window"), "occurrence_id": payload.get("stable_key") or cid})
    return {"accepted": usable, "usable": usable, "fact": fact, "assertions": assertions,
            "issues": [{"code": code} for code in sorted(set(codes))],
            "candidate_state": "accepted" if usable else "unresolved"}
