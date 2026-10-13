"""Offline score diagnostics on independently verified facts; never calibrates scores.

From the repository root:
  backend/.venv/Scripts/python.exe backend/scripts/confidence_diagnostics.py \
    --gold annotations.json --predictions predictions.json --out diagnostics.json
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.agent_evaluation import (  # noqa: E402
    evidence_matches,
    fact_key,
    ratio,
    read_json,
    unique_facts,
    validate_annotations,
    write_json,
)


def _fact(row: dict) -> dict:
    """Accept evaluation facts and the platform's automatic fact-view export."""
    return {
        **row,
        "raw_value": row.get("raw_value", row.get("raw", row.get("value", ""))),
        "normalized_value": row.get("normalized_value", row.get("normalized", row.get("value", row.get("raw_value", row.get("raw", ""))))),
        "dimensions": row.get("dimensions", {}),
    }


def _fields(document: dict) -> dict[str, dict]:
    if "fields" in document:
        fields = document["fields"]
        if len({f["field_key"] for f in fields}) != len(fields):
            raise ValueError(f"Duplicate prediction field: {document.get('id')}")
        return {f["field_key"]: f for f in fields}
    fields: dict[str, dict] = {}
    for fact in document.get("facts", []):
        key = fact["field_key"]
        fields.setdefault(key, {"field_key": key, "facts": []})["facts"].append(fact)
    return fields


def _confidence(fact: dict) -> dict:
    raw = fact.get("confidence")
    if not isinstance(raw, dict):
        raw = {"score": raw, "kind": "legacy_method" if raw is not None else "unknown"}
    score = raw.get("score")
    if score is not None and (
        isinstance(score, bool) or not isinstance(score, int | float)
        or not math.isfinite(score) or not 0 <= score <= 1
    ):
        raise ValueError("Confidence score must be null or a finite number in [0,1]")
    if any(raw.get(key) is not None and not isinstance(raw[key], str) for key in ("kind", "version", "semantics")):
        raise ValueError("Confidence kind/version/semantics must be strings or null")
    return {"score": score, "kind": raw.get("kind") or "unknown",
            "version": raw.get("version"), "semantics": raw.get("semantics"),
            "calibration_scope": raw.get("calibration_scope")}


def _contaminated(metadata: dict) -> bool:
    return any(metadata.get(key) is True for key in (
        "holdout_contaminated", "used_for_tuning", "tuned_on_holdout", "answers_exposed"
    )) or any(isinstance(value, dict) and _contaminated(value) for value in metadata.values())


def _evidence_supported(prediction: dict, expected: dict, threshold: float | None) -> bool:
    compatible = [p for p in prediction.get("evidence", []) if any(
        p.get("page_number") == g.get("page_number") and p.get("role") == g.get("role")
        and all(p.get(k) is None or g.get(k) is None or p[k] == g[k]
                for k in ("coordinate_space", "page_rotation"))
        for g in expected.get("evidence", []))]
    return evidence_matches({**prediction, "evidence": compatible}, expected, threshold)


def diagnose(
    gold: dict,
    predictions: dict,
    *,
    bins: int = 10,
    min_samples: int | None = None,
    evidence_iou: float | None = None,
    expected_confidence_version: str | None = None,
) -> dict:
    """Report empirical behaviour, retaining unknowns and scope limitations.

    min_samples is an explicit study design parameter, not a deployment threshold.
    Without it, probability-specific metrics remain undefined.
    """
    if isinstance(bins, bool) or not isinstance(bins, int) or not 1 <= bins <= 100:
        raise ValueError("bins must be an integer between 1 and 100")
    if min_samples is not None and (isinstance(min_samples, bool) or not isinstance(min_samples, int) or min_samples < 2):
        raise ValueError("min_samples must be an explicit integer >=2")
    if evidence_iou is not None and not 0 <= evidence_iou <= 1:
        raise ValueError("evidence_iou must be between 0 and 1")
    errors = validate_annotations(gold)
    if errors:
        raise ValueError("Invalid annotations: " + "; ".join(errors))
    expected = {str(doc["id"]): doc for doc in gold.get("documents", [])}
    docs = predictions.get("documents", [])
    predicted = {str(doc.get("id", doc.get("document_id"))): doc for doc in docs}
    if len(predicted) != len(docs):
        raise ValueError("Duplicate prediction document id")
    if set(predicted) - set(expected):
        raise ValueError("Prediction document is outside gold scope")
    metadata = predictions.get("run_metadata", {})
    expected_configuration = gold.get("evaluation_contract", {}).get("frozen_configuration")
    if expected_configuration is not None and metadata.get("frozen_configuration") != expected_configuration:
        raise ValueError("Prediction frozen configuration does not match evaluation contract")
    versions: dict[str, set] = defaultdict(set)
    observed_metadata: dict[str, str] = {}
    totals: Counter = Counter()
    labels = []
    missing = []
    outcomes: Counter = Counter()
    scored_documents = set()
    scored_scope: Counter = Counter()
    for doc_id, doc in expected.items():
        predicted_doc = predicted.get(doc_id, {})
        outcomes[predicted_doc.get("status", "PRESENT" if predicted_doc else "MISSING")] += 1
        if predicted_doc and predicted_doc.get("sha256", predicted_doc.get("file_hash")) != doc["sha256"]:
            raise ValueError(f"Prediction file hash mismatch: {doc_id}")
        doc_metadata = predicted_doc.get("run_metadata", {})
        for key in ("frozen_configuration", "model", "model_version", "knowledge_hash"):
            if key in doc_metadata and key in metadata and doc_metadata[key] != metadata[key]:
                raise ValueError(f"Mixed prediction version metadata: {key}")
            if key in doc_metadata:
                current = json.dumps(doc_metadata[key], sort_keys=True)
                if key in observed_metadata and observed_metadata[key] != current:
                    raise ValueError(f"Mixed prediction version metadata: {key}")
                observed_metadata[key] = current
        if doc.get("split") == "holdout" and (
            _contaminated(metadata) or _contaminated(doc_metadata) or _contaminated(doc)
            or _contaminated(gold.get("run_metadata", {}))
        ):
            raise ValueError("Contaminated holdout cannot be used as independent confidence validation")
        fields = _fields(predicted_doc)
        if set(fields) - {f["field_key"] for f in doc.get("fields", [])}:
            raise ValueError(f"Prediction field outside annotated field scope: {doc_id}")
        for field in doc.get("fields", []):
            raw_predictions = [_fact(f) for f in fields.get(field["field_key"], {}).get("facts", [])]
            if field["status"] not in {"VERIFIED", "ABSENT"}:
                totals["unscored_fields"] += 1
                totals["unscored_predictions"] += len(raw_predictions)
                continue
            scored_documents.add(doc_id)
            scored_scope[field["scope"]] += 1
            totals["scored_fields"] += 1
            # Group repeated evidence for a fact, but reject contradictory score metadata.
            seen_confidence = {}
            for fact in raw_predictions:
                confidence = _confidence(fact)
                key = fact_key(fact)
                if key in seen_confidence and seen_confidence[key] != confidence:
                    raise ValueError(f"Duplicate fact has conflicting confidence metadata: {doc_id}:{field['field_key']}")
                seen_confidence[key] = confidence
            facts = unique_facts(raw_predictions)
            expected_facts = unique_facts(field.get("facts", []))
            unmatched = list(range(len(expected_facts)))
            for fact in facts:
                if field["scope"] == "sampled_pages" and fact.get("evidence") and all(
                    e.get("page_number") not in field["scope_pages"] for e in fact["evidence"]
                ):
                    totals["outside_scope_predictions"] += 1
                    continue
                confidence = seen_confidence[fact_key(fact)]
                kind, version = confidence["kind"], confidence["version"]
                if expected_confidence_version is not None and version != expected_confidence_version:
                    raise ValueError("Confidence version does not match requested evaluation version")
                if confidence["score"] is not None:
                    versions[kind].add(version)
                match = next((i for i in unmatched if fact_key(fact) == fact_key(expected_facts[i])), None)
                correct = match is not None
                supported = False
                if match is not None:
                    unmatched.remove(match)
                    supported = _evidence_supported(fact, expected_facts[match], evidence_iou)
                totals["tp" if correct else "fp"] += 1
                totals["evidence_supported_tp"] += supported
                totals["unknown_score"] += confidence["score"] is None
                labels.append({"document_id": doc["id"], "field_key": field["field_key"],
                               "split": doc.get("split", "unknown"), "layout": doc.get("layout", "unknown"),
                               "mine_group": doc.get("mine_group", doc.get("mine", "unknown")),
                               "scope": field["scope"], "scope_pages": field.get("scope_pages", []),
                               "correct": correct, "evidence_supported": supported,
                               "confidence": confidence, "fact": fact})
            totals["fn"] += len(unmatched)
            totals["gold_facts"] += len(expected_facts)
            missing.extend({"document_id": doc["id"], "field_key": field["field_key"], "fact": expected_facts[i]}
                           for i in unmatched)
    if any(len(values) > 1 for values in versions.values()):
        raise ValueError("Mixed confidence versions; evaluate each score version separately")
    groups: dict[tuple, list] = defaultdict(list)
    for row in labels:
        confidence = row["confidence"]
        key = (confidence["kind"], confidence["version"], row["field_key"], confidence["semantics"])
        groups[key].append(row)
    grouped = [_group_report(key, rows, bins, min_samples) for key, rows in sorted(groups.items(), key=lambda kv: str(kv[0]))]
    warnings = ["Empirical precision describes these verified samples only; no score is calibrated by this tool.",
                "This report creates no auto-release policy or business correctness guarantee."]
    if scored_scope["sampled_pages"]:
        warnings.append("Sampled-page gold cannot establish whole-document recall.")
    if any(row["split"] == "unknown" for row in labels):
        warnings.append("Some samples lack split metadata; independent validation is not established.")
    if len(scored_documents) < 5:
        warnings.append("Fewer than 5 scored documents; avoid production generalisation claims.")
    if not labels:
        warnings.append("No scoreable predicted facts; no confidence conclusion is available.")
    if any(not doc.get("page_bounds") for doc in expected.values()):
        warnings.append("Some gold records lack physical page bounds; this report does not establish in-page geometry validity.")
    summary = {**dict(totals), "documents": len(expected), "scored_documents": len(scored_documents),
               "predicted_facts": len(labels), "unknown_score": totals["unknown_score"],
               "tp": totals["tp"], "fp": totals["fp"], "fn": totals["fn"],
               "precision": ratio(totals["tp"], totals["tp"] + totals["fp"]),
               "recall_within_annotated_scope": ratio(totals["tp"], totals["tp"] + totals["fn"])}
    return {"schema_version": 1, "status": "DIAGNOSTIC" if totals["scored_fields"] else "NO_VERIFIED_GOLD",
            "summary": summary, "groups": grouped, "labels": labels, "false_negative": missing,
            "document_outcomes": dict(outcomes), "scored_scope": dict(scored_scope),
            "warnings": warnings, "run_metadata": metadata,
            "study_design": {"bins": bins, "min_samples": min_samples, "evidence_iou": evidence_iou,
                             "min_samples_meaning": "study design parameter; not an auto-release threshold"}}


def _group_report(key: tuple, rows: list[dict], bins: int, min_samples: int | None) -> dict:
    kind, version, field_key, semantics = key
    scored = [row for row in rows if row["confidence"]["score"] is not None]
    by_bin: dict[int, list] = defaultdict(list)
    for row in scored:
        index = min(int(row["confidence"]["score"] * bins), bins - 1)
        by_bin[index].append(row)
    bin_rows = []
    for index, values in sorted(by_bin.items()):
        correct = sum(row["correct"] for row in values)
        bin_rows.append({"index": index, "lower_inclusive": index / bins,
                         "upper": (index + 1) / bins, "upper_inclusive": index == bins - 1,
                         "n": len(values), "documents": len({str(row['document_id']) for row in values}),
                         "correct": correct, "empirical_precision": correct / len(values),
                         "mean_score": sum(row["confidence"]["score"] for row in values) / len(values),
                         "evidence_supported": sum(row["evidence_supported"] for row in values)})
    reasons = []
    probability_kind = kind in {"declared_probability", "calibrated_probability"}
    if not probability_kind or semantics != "fact_correct_probability":
        reasons.append("Score is a method/model diagnostic or lacks explicit fact-correct-probability semantics")
    if not version:
        reasons.append("Score version is missing")
    if min_samples is None:
        reasons.append("No minimum sample count was predeclared in the study design")
    elif len(scored) < min_samples:
        reasons.append("Scoreable facts are fewer than the predeclared minimum")
    if not scored or not any(row["correct"] for row in scored) or all(row["correct"] for row in scored):
        reasons.append("Both correct and incorrect verified outcomes are required for this feasibility report")
    if any(row["split"] != "holdout" for row in scored):
        reasons.append("Scores are not all from an independent holdout split")
    # No fitting, relabelling or output probabilities. These are validation metrics only.
    brier = sum((row["confidence"]["score"] - int(row["correct"])) ** 2 for row in scored) / len(scored) if not reasons else None
    ece = sum(abs(row["mean_score"] - row["empirical_precision"]) * row["n"] for row in bin_rows) / len(scored) if not reasons else None
    return {"kind": kind, "version": version, "field_key": field_key, "semantics": semantics,
            "status": "PROBABILITY_VALIDATION_ONLY" if not reasons else "METHOD_DIAGNOSTIC",
            "n": len(rows), "scored_n": len(scored), "unknown_n": len(rows) - len(scored),
            "documents": len({str(row['document_id']) for row in rows}),
            "correct": sum(row["correct"] for row in rows),
            "empirical_precision": ratio(sum(row["correct"] for row in rows), len(rows)),
            "bins": bin_rows, "brier": brier, "ece": ece,
            "undefined_reasons": reasons,
            "scope": {"splits": dict(Counter(row["split"] for row in rows)),
                      "layouts": dict(Counter(row["layout"] for row in rows)),
                      "mine_groups": dict(Counter(row["mine_group"] for row in rows))},
            "calibration_performed": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", required=True, type=Path)
    parser.add_argument("--predictions", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--bins", type=int, default=10)
    parser.add_argument("--min-samples", type=int, help="predeclared study minimum; no default or deployment threshold")
    parser.add_argument("--evidence-iou", type=float)
    parser.add_argument("--expected-confidence-version")
    args = parser.parse_args(argv)
    result = diagnose(read_json(args.gold), read_json(args.predictions), bins=args.bins,
                      min_samples=args.min_samples, evidence_iou=args.evidence_iou,
                      expected_confidence_version=args.expected_confidence_version)
    write_json(args.out, result)
    print(json.dumps({"status": result["status"], "summary": result["summary"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
