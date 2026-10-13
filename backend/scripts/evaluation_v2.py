"""可靠提取的 gold-matrix-v2 评分器。

本模块有意独立于旧版清单评分器。它对封闭的 document x rule 矩阵评分，
并将未完成工作作为可度量结果，而不是静默从分母中删除。
"""

from __future__ import annotations

import argparse
import json
import math
import re
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from scripts.eval.gold_matrix_v2 import validate_gold_matrix

_MANUAL_MARKERS = {
    "manual", "human", "human_review", "revision", "review",
    "human_correction", "human_corrected", "manual_correction", "manual_corrected",
}


def _load_json(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def _number(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    if not isinstance(value, str | int | float):
        return None
    text = str(value).strip()
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _norm(value: Any) -> str:
    """只规范化无影响的文本和数字格式。

    单位换算不会被自动推断。未来契约扩展中，规则可以显式启用 unit_conversions 表。
    """

    number = _number(value)
    if number is not None:
        return str(number.normalize())
    return re.sub(r"\s+", "", str(value).strip()).casefold()


def _same(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return a is b
    if isinstance(a, dict) or isinstance(b, dict):
        return isinstance(a, dict) and isinstance(b, dict) and set(a) == set(b) and all(
            _same(a[key], b[key]) for key in a
        )
    if isinstance(a, list) or isinstance(b, list):
        return isinstance(a, list) and isinstance(b, list) and len(a) == len(b) and all(
            _same(left, right) for left, right in zip(a, b, strict=True)
        )
    return _norm(a) == _norm(b)


def _canonical_location(item: dict[str, Any]) -> tuple[Any, ...]:
    bbox = item.get("bbox")
    if isinstance(bbox, list):
        bbox_key = tuple(round(float(v), 5) if isinstance(v, int | float) else v for v in bbox)
    else:
        bbox_key = None
    return (
        item.get("page_number"),
        bbox_key,
        _norm(item.get("quote", "")),
        item.get("coordinate_system"),
    )


def _occurrences(fact: dict[str, Any]) -> list[dict[str, Any]]:
    values = fact.get("occurrences")
    if isinstance(values, list):
        return [item for item in values if isinstance(item, dict)]
    # 较旧的预测适配器可能将这些字段称为 evidence。
    # 将其按位置接受，同时保持 gold-matrix 契约以出现项为先。
    values = fact.get("evidence")
    return [item for item in values if isinstance(item, dict)] if isinstance(values, list) else []


def _attribute_evidence(fact: dict[str, Any], attribute: str) -> list[Any]:
    evidence = fact.get("attribute_evidence")
    if not isinstance(evidence, dict):
        return []
    if attribute.startswith("dimensions."):
        dimensions = evidence.get("dimensions") or {}
        value = dimensions.get(attribute.split(".", 1)[1], []) if isinstance(dimensions, dict) else []
    else:
        value = evidence.get(attribute, [])
    if isinstance(value, dict):
        return [value]
    return value if isinstance(value, list) else []


def _registry(fact: dict[str, Any]) -> dict[str, dict[str, Any]]:
    registry = {str(item.get("occurrence_id")): item for item in _occurrences(fact)}
    evidence = fact.get("evidence_registry", [])
    if isinstance(evidence, dict):
        evidence = [dict(item, evidence_id=key) for key, item in evidence.items()
                    if isinstance(item, dict)]
    if isinstance(evidence, list):
        registry.update({str(item.get("evidence_id", item.get("occurrence_id"))): item
                         for item in evidence if isinstance(item, dict)})
    return registry


def _valid_location(item: dict[str, Any]) -> bool:
    bbox = item.get("bbox")
    page = item.get("page_number")
    return (item.get("coordinate_system") == "PDF_POINT" and isinstance(page, int)
            and not isinstance(page, bool) and page >= 1
            and isinstance(bbox, list) and len(bbox) == 4
            and all(isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v)
                    for v in bbox)
            and bbox[0] >= 0 and bbox[1] >= 0 and bbox[2] > bbox[0] and bbox[3] > bbox[1]
            and isinstance(item.get("quote"), str) and bool(item["quote"].strip()))


def _location_matches(pred: dict[str, Any], gold: dict[str, Any], threshold: float) -> bool:
    if not _valid_location(pred) or not _valid_location(gold):
        return False
    if pred["page_number"] != gold["page_number"] or not _same(pred["quote"], gold["quote"]):
        return False
    a, b = pred["bbox"], gold["bbox"]
    intersection = max(0, min(a[2], b[2]) - max(a[0], b[0])) \
        * max(0, min(a[3], b[3]) - max(a[1], b[1]))
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - intersection
    return bool(union > 0 and intersection / union >= threshold)


def _evidence_supported(pred: dict[str, Any], gold: dict[str, Any], attr: str,
                        threshold: float) -> bool:
    required = _attribute_evidence(gold, attr)
    if not required:
        return True
    provided = _attribute_evidence(pred, attr)
    pred_registry, gold_registry = _registry(pred), _registry(gold)
    return any(_location_matches(pred_registry[p], gold_registry[g], threshold)
               for p in provided for g in required
               if isinstance(p, str) and isinstance(g, str)
               and p in pred_registry and g in gold_registry)


def _value_unit(fact: dict[str, Any], rule: dict[str, Any]) -> tuple[Any, Any]:
    value, unit = fact.get("value", fact.get("normalized_value")), fact.get("unit")
    policy = rule.get("unit_policy") or {}
    if not isinstance(policy, dict):
        return value, unit
    aliases = policy.get("aliases", {})
    if isinstance(aliases, dict):
        unit = aliases.get(str(unit), unit)
    for conversion in policy.get("conversions", []):
        if not isinstance(conversion, dict) or not _same(unit, conversion.get("from")):
            continue
        number, factor = _number(value), _number(conversion.get("factor"))
        offset = _number(conversion.get("offset", 0))
        if number is None or factor is None or offset is None:
            continue
        return str(number * factor + offset), conversion.get("to")
    return value, unit


def _fact_matches(pred: dict[str, Any], gold: dict[str, Any], rule: dict[str, Any],
                  threshold: float) -> bool:
    if pred.get("source_validity") in {"invalid", "invalidated", "superseded"} \
            or pred.get("source_valid") is False:
        return False
    pred_value, pred_unit = _value_unit(pred, rule)
    gold_value, gold_unit = _value_unit(gold, rule)
    if not _same(pred_value, gold_value) or not _same(pred_unit, gold_unit):
        return False
    for name in ("value_kind", "source_role", "table_relation"):
        expected = gold.get(name)
        if expected is not None and not _same(pred.get(name), expected):
            return False
    dimensions = gold.get("dimensions") or {}
    predicted_dimensions = pred.get("dimensions") or {}
    if any(not _same(predicted_dimensions.get(key), value) for key, value in dimensions.items()):
        return False
    for attr in ("value", "unit", "value_kind", "source_role", "table_relation"):
        if not _evidence_supported(pred, gold, attr, threshold):
            return False
    for key in dimensions:
        if not _evidence_supported(pred, gold, f"dimensions.{key}", threshold):
            return False
    for attr in gold.get("attribute_evidence", {}):
        if attr != "dimensions" and not _evidence_supported(pred, gold, attr, threshold):
            return False
    return True


def _manual(item: dict[str, Any]) -> bool:
    candidates = [
        item.get("provenance"), item.get("origin"), item.get("source"),
        item.get("human_review_status"), item.get("review_source"), item.get("source_kind"),
        item.get("original_source"), item.get("original_source_kind"),
    ]
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.casefold() in _MANUAL_MARKERS:
            return True
        if isinstance(candidate, dict):
            if any(str(candidate.get(key, "")).casefold() in _MANUAL_MARKERS
                   for key in ("kind", "source", "type", "status", "source_kind", "original_source")):
                return True
    return bool(item.get("manual") is True or item.get("human_review") is True
                or item.get("origin_source_revision_id"))


def _determinate_facts(rule: dict[str, Any]) -> list[dict[str, Any]]:
    facts = rule.get("facts")
    if not isinstance(facts, list):
        return []
    result = []
    for fact in facts:
        if not isinstance(fact, dict):
            continue
        if fact.get("determination") in {False, "candidate", "pending", "uncertain"}:
            continue
        if fact.get("is_determined") is False:
            continue
        result.append(fact)
    return result


def _prediction_map(payload: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for document in payload.get("documents", []):
        if not isinstance(document, dict):
            continue
        doc_id = str(document.get("document_id", document.get("id")))
        rules = document.get("rules", document.get("fields", []))
        if isinstance(rules, dict):
            rules = [dict(value, field_key=key) for key, value in rules.items()]
        for rule in rules if isinstance(rules, list) else []:
            if isinstance(rule, dict):
                field = str(rule.get("field_key", rule.get("rule_id")))
                if (doc_id, field) in result:
                    raise ValueError(f"Duplicate prediction task: {doc_id}/{field}")
                result[(doc_id, field)] = rule
    return result


def _gold_fact_list(rule: dict[str, Any]) -> list[dict[str, Any]]:
    facts = rule.get("expected_facts", rule.get("facts", []))
    return [item for item in facts if isinstance(item, dict)] if isinstance(facts, list) else []


def _occurrence_complete(pred: list[dict[str, Any]], gold: list[dict[str, Any]],
                         threshold: float) -> bool:
    observed = {_canonical_location(item): item for fact in pred for item in _occurrences(fact)}
    expected = {_canonical_location(item): item for item in gold}
    matches = _maximum_matching(list(observed.values()), list(expected.values()),
                                lambda p, g: _location_matches(p, g, threshold))
    return len(matches) == len(observed) == len(expected)


def _all_occurrences_match(observed: list[dict[str, Any]], expected: list[dict[str, Any]],
                           rule: dict[str, Any], threshold: float) -> bool:
    def complete_pair(pred, gold):
        return _fact_matches(pred, gold, rule, threshold) and _occurrence_complete(
            [pred], _occurrences(gold), threshold
        )

    return len(_maximum_matching(observed, expected, complete_pair)) == len(expected) \
        and all(any(complete_pair(pred, fact) for fact in expected) for pred in observed)


def _wrong_occurrences(observed: list[dict[str, Any]], expected: list[dict[str, Any]],
                        rule: dict[str, Any], threshold: float) -> int:
    return sum(not any(_fact_matches(pred, fact, rule, threshold)
                       and any(_location_matches(location, target, threshold)
                               for target in _occurrences(fact)) for fact in expected)
               for pred in observed for location in _occurrences(pred))


def _maximum_matching(observed: list[Any], expected: list[Any], predicate) -> dict[int, int]:
    edges = [[index for index, fact in enumerate(expected) if predicate(pred, fact)]
             for pred in observed]
    assigned: dict[int, int] = {}

    def assign(pred_index: int, visited: set[int]) -> bool:
        for gold_index in edges[pred_index]:
            if gold_index in visited:
                continue
            visited.add(gold_index)
            if gold_index not in assigned or assign(assigned[gold_index], visited):
                assigned[gold_index] = pred_index
                return True
        return False

    for pred_index in range(len(observed)):
        assign(pred_index, set())
    return assigned


def _match_facts(expected: list[dict[str, Any]], observed: list[dict[str, Any]],
                 rule: dict[str, Any], threshold: float) -> tuple[int, int, int, int]:
    # 最大二分匹配避免在两个 gold 事实属性相同但可接受证据不同时，
    # 因顺序而产生的贪心分配差异。
    def predicate(p, g):
        return _fact_matches(p, g, rule, threshold)
    assigned = _maximum_matching(observed, expected, predicate)
    matched = set(assigned.values())
    duplicates = sum(any(predicate(observed[index], fact) for fact in expected)
                     for index in range(len(observed)) if index not in matched)
    tp = len(assigned)
    return tp, len(observed) - tp - duplicates, len(expected) - tp, duplicates


def evaluate(gold: dict[str, Any], predictions: dict[str, Any], *,
             evidence_iou: float = 1.0, require_adjudicated: bool = False) -> dict[str, Any]:
    """对 gold-matrix-v2 载荷和预测导出结果评分。

    返回的 summary 包含固定的文档 x 规则任务数。待处理或草稿裁定记录会单独报告，
    永远不进入自动存在或不存在分母。
    """

    errors = validate_gold_matrix(gold, require_adjudicated=require_adjudicated)
    if errors:
        raise ValueError("Invalid gold matrix: " + "; ".join(errors))
    if isinstance(evidence_iou, bool) or not isinstance(evidence_iou, int | float) \
            or not math.isfinite(evidence_iou) or not 0 < evidence_iou <= 1:
        raise ValueError("evidence_iou must be a finite number in (0, 1]")
    rules = {str(item.get("field_key", item.get("rule_id"))): item
             for item in gold.get("rules", []) if isinstance(item, dict)}
    gold_documents = gold.get("documents", [])
    prediction_index = _prediction_map(predictions)
    prediction_documents = {str(doc.get("document_id", doc.get("id"))): doc
                            for doc in predictions.get("documents", []) if isinstance(doc, dict)}
    rows: list[dict[str, Any]] = []
    totals: Counter[str] = Counter()
    expected_task_keys = set()
    document_results: dict[str, list[bool]] = {}
    document_eligible: dict[str, bool] = {}
    for document in gold_documents:
        if not isinstance(document, dict):
            continue
        doc_id = str(document.get("document_id", document.get("id")))
        pred_document = prediction_documents.get(doc_id, {})
        expected_hash = document.get("source", {}).get("sha256")
        predicted_hash = pred_document.get("source", {}).get("sha256", pred_document.get("sha256"))
        if predicted_hash is not None and expected_hash is not None and predicted_hash != expected_hash:
            raise ValueError(f"Prediction document hash mismatch: {doc_id}")
        document_eligible[doc_id] = True
        doc_rules = {str(item.get("field_key", item.get("rule_id"))): item
                     for item in document.get("rules", []) if isinstance(item, dict)}
        fields = list(rules) if rules else list(doc_rules)
        for field_key in fields:
            gold_rule = doc_rules.get(field_key, {})
            expected_task_keys.add((doc_id, field_key))
            pred_rule = prediction_index.get((doc_id, field_key), {})
            totals["planned_tasks"] += pred_rule.get("job_created") is True
            totals["attempted_tasks"] += pred_rule.get("attempted") is True
            totals["range_closed_tasks"] += pred_rule.get("range_closed") is True
            answerability = gold_rule.get("answerability")
            adjudication = gold_rule.get("adjudication") or {}
            adjudicated = adjudication.get("status") == "adjudicated"
            row: dict[str, Any] = {"document_id": doc_id, "field_key": field_key,
                                   "answerability": answerability, "adjudicated": adjudicated}
            totals["tasks"] += 1
            if not adjudicated:
                totals["pending_adjudication_tasks"] += 1
                row["status"] = "pending_adjudication"
                rows.append(row)
                document_eligible[doc_id] = False
                continue
            if answerability in {"unreadable", "ambiguous", "out_of_scope"}:
                totals[f"{answerability}_tasks"] += 1
                row["status"] = answerability
                rows.append(row)
                if answerability in {"unreadable", "ambiguous"}:
                    document_eligible[doc_id] = False
                continue
            if answerability not in {"present", "absent"}:
                raise ValueError(f"Invalid adjudicated answerability: {doc_id}/{field_key}")
            totals["adjudicated_present_absent_tasks"] += 1
            expected = _gold_fact_list(gold_rule)
            all_observed = _determinate_facts(pred_rule)
            manual_rule = _manual(pred_rule) or _manual(predictions) or _manual(pred_document)
            observed = [fact for fact in all_observed if not manual_rule and not _manual(fact)]
            manual = manual_rule or len(observed) != len(all_observed)
            semantic_state = pred_rule.get("semantic_state", pred_rule.get("status", "unknown"))
            if "rule_semantic_hash" in pred_rule \
                    and pred_rule["rule_semantic_hash"] != rules[field_key].get("rule_semantic_hash"):
                raise ValueError(f"Prediction rule snapshot mismatch: {doc_id}/{field_key}")
            if pred_rule.get("source_validity") in {"invalid", "invalidated", "superseded"} \
                    or pred_rule.get("source_valid") is False:
                observed = [dict(fact, source_valid=False) for fact in observed]
            tp, fp, fn, duplicates = _match_facts(expected, observed, rules[field_key], evidence_iou)
            range_closed = pred_rule.get("range_closed") is True
            proof_valid = pred_rule.get("proof_valid") is True
            source_valid = (pred_rule.get("source_validity") not in {"invalid", "invalidated", "superseded"}
                            and pred_rule.get("source_valid") is not False)
            absent_ok = answerability == "absent" and semantic_state == "not_found" \
                and range_closed and proof_valid and not all_observed and not manual and source_valid
            present_complete = (answerability == "present" and semantic_state == "completed"
                                and tp == len(expected) and fp == 0 and duplicates == 0 and fn == 0
                                and range_closed and not manual)
            if gold_rule.get("cardinality") == "all_occurrences" and present_complete:
                present_complete = _all_occurrences_match(observed, expected, rules[field_key], evidence_iou)
            wrong_occurrences = (_wrong_occurrences(observed, expected, rules[field_key], evidence_iou)
                                 if gold_rule.get("cardinality") == "all_occurrences" else 0)
            auto_correct = absent_ok or present_complete
            if auto_correct:
                totals["automatic_correct_tasks"] += 1
            totals["tp"] += tp
            totals["fp"] += fp
            totals["fn"] += fn
            totals["duplicate_outputs"] += duplicates
            totals["wrong_occurrences"] += wrong_occurrences
            totals["manual_facts_excluded"] += len(all_observed) - len(observed)
            totals["gold_present_facts"] += len(expected) if answerability == "present" else 0
            totals[f"{semantic_state}_tasks"] += 1
            if observed:
                totals["determinate_positive_outputs"] += len(observed)
            if manual:
                totals["manual_output_tasks"] += 1
            # 已存在规则回答 not_found 属于错误地判定不存在；
            # 不存在的规则却有事实，属于错误判定。
            wrong_determination = not manual_rule and (
                (answerability == "present" and semantic_state == "not_found")
                or (answerability == "absent" and bool(observed))
            )
            if wrong_determination or fp > 0 or duplicates > 0 or wrong_occurrences > 0:
                totals["task_error_tasks"] += 1
            document_results.setdefault(doc_id, []).append(auto_correct)
            row.update({"status": "automatic_correct" if auto_correct else "not_automatic_correct",
                        "semantic_state": semantic_state, "tp": tp, "fp": fp, "fn": fn,
                        "range_closed": range_closed, "proof_valid": proof_valid,
                        "manual": manual, "determinate_facts": len(observed),
                        "duplicate_outputs": duplicates,
                        "wrong_occurrences": wrong_occurrences,
                        "manual_facts_excluded": len(all_observed) - len(observed)})
            rows.append(row)

    positive_total = sum(1 for row in rows if row.get("answerability") == "present" and row.get("adjudicated"))
    absent_total = sum(1 for row in rows if row.get("answerability") == "absent" and row.get("adjudicated"))
    summary: dict[str, Any] = dict(totals)
    for key in ("tasks", "planned_tasks", "attempted_tasks", "range_closed_tasks",
                "pending_adjudication_tasks", "adjudicated_present_absent_tasks",
                "automatic_correct_tasks", "tp", "fp", "fn", "duplicate_outputs",
                "gold_present_facts", "determinate_positive_outputs", "manual_output_tasks",
                "task_error_tasks", "wrong_occurrences", "manual_facts_excluded"):
        summary.setdefault(key, 0)
    evaluable_documents = {doc: values for doc, values in document_results.items()
                           if document_eligible[doc] and values}
    summary.update({
        "document_rule_tasks": totals["tasks"],
        "planned_coverage_rate": totals["planned_tasks"] / totals["tasks"] if totals["tasks"] else None,
        "execution_record_rate": totals["attempted_tasks"] / totals["tasks"] if totals["tasks"] else None,
        "range_closed_rate": totals["range_closed_tasks"] / totals["tasks"] if totals["tasks"] else None,
        "fact_recall": totals["tp"] / totals["gold_present_facts"] if totals["gold_present_facts"] else None,
        "determinate_output_precision": (totals["tp"] / totals["determinate_positive_outputs"]
                                          if totals["determinate_positive_outputs"] else None),
        "automatic_correct_completion_rate": (totals["automatic_correct_tasks"]
                                               / (positive_total + absent_total)
                                               if positive_total + absent_total else None),
        "task_error_rate": (totals["task_error_tasks"] / (positive_total + absent_total)
                            if positive_total + absent_total else None),
        "not_found_error_rate": (sum(1 for row in rows if row.get("answerability") == "present"
                                      and row.get("semantic_state") == "not_found") / positive_total
                                 if positive_total else None),
        "positive_rule_tasks": positive_total,
        "absent_rule_tasks": absent_total,
        "evaluable_documents": len(evaluable_documents),
        "automatic_complete_correct_documents": sum(all(values) for values in evaluable_documents.values()),
        "document_complete_correct_rate": (sum(all(values) for values in evaluable_documents.values())
                                           / len(evaluable_documents) if evaluable_documents else None),
        "not_found_output_error_rate": (sum(row.get("answerability") == "present" for row in rows
                                             if row.get("semantic_state") == "not_found")
                                         / totals["not_found_tasks"] if totals["not_found_tasks"] else None),
    })
    return {"schema_version": "evaluation-v2", "status": "EVALUATED" if positive_total + absent_total else "NO_ADJUDICATED_GOLD",
            "summary": summary, "rows": rows,
            "outside_matrix_prediction_tasks": [list(key) for key in sorted(set(prediction_index) - expected_task_keys)]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", required=True, type=Path)
    parser.add_argument("--predictions", required=True, type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--require-adjudicated", action="store_true")
    parser.add_argument("--evidence-iou", type=float, default=1.0)
    args = parser.parse_args()
    result = evaluate(_load_json(args.gold), _load_json(args.predictions),
                      evidence_iou=args.evidence_iou, require_adjudicated=args.require_adjudicated)
    encoded = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        args.out.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)


if __name__ == "__main__":
    main()
