"""Gold matrix v2 schema, loader and validation helpers.

This module deliberately does not infer truth from extraction output.  The
legacy checklist can be migrated into a review template, but its values are
kept as ``legacy_observations`` until a human adjudicator verifies the source
document.  The module has no dependency on the production ORM or parser.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "gold-matrix-v2"
EVALUATION_CONTRACT_VERSION = "gold-matrix-v2"
ANSWERABILITY = {"present", "absent", "unreadable", "ambiguous", "out_of_scope"}
ADJUDICATION_STATES = {"pending", "draft", "adjudicated"}
CARDINALITIES = {"single", "multi_identity", "all_occurrences"}
SCOPES = {"whole_document", "attachment", "sections", "explicit_ranges", "sampled_pages"}
SOURCE_LAYOUTS = {"native", "mixed", "scan", "unknown"}
OCCURRENCE_COORDINATE_SYSTEM = "PDF_POINT"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def semantic_hash(rule: Mapping[str, Any]) -> str:
    """Return a stable hash for the rule semantics used by the gold matrix."""

    fields = (
        "field_key", "cardinality", "scope", "identity_dimensions",
        "applicable_dimensions", "value_kind_policy", "unit_policy",
        "retrieval_terms", "synonym_terms", "ocr_variants", "formula_patterns",
        "table_patterns", "evidence_requirements",
    )
    payload = {key: rule.get(key) for key in fields}
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _rule_semantics(rule: Mapping[str, Any]) -> dict[str, Any]:
    extract = (rule.get("strategy") or {}).get("agent_extract") or {}
    field_key = rule.get("field_key")
    identities = extract.get("identity_dimensions") or []
    evidence = extract.get("evidence_requirements") or []
    synonyms = extract.get("synonyms") or []
    snapshot = {
        "field_key": field_key,
        "rule_id": rule.get("rule_id"),
        "rule_version": rule.get("version", 1),
        "field_name": rule.get("field_name_cn") or field_key,
        "cardinality": rule.get("cardinality") or (
            "all_occurrences" if rule.get("multi_instance") else "single"
        ),
        "scope": rule.get("scope") or "whole_document",
        "identity_dimensions": list(identities),
        "applicable_dimensions": list(rule.get("applicable_dimensions") or identities),
        "value_kind_policy": rule.get("value_kind_policy") or {
            "expected": rule.get("field_name_cn") or field_key,
        },
        "unit_policy": rule.get("unit_policy") or {"expected": rule.get("unit")},
        "retrieval_terms": list(rule.get("retrieval_terms") or [field_key]),
        "synonym_terms": list(synonyms),
        "ocr_variants": list(rule.get("ocr_variants") or []),
        "formula_patterns": list(rule.get("formula_patterns") or []),
        "table_patterns": list(rule.get("table_patterns") or []),
        "evidence_requirements": list(evidence),
    }
    snapshot["rule_semantic_hash"] = semantic_hash(snapshot)
    return snapshot


def normalize_rule_catalog(rules: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Convert the legacy rule checklist into the frozen gold rule shape."""

    return [_rule_semantics(rule) for rule in rules]


def _source_metadata(document: Mapping[str, Any]) -> dict[str, Any]:
    """Copy only observed metadata; unknown source facts remain null."""

    pages = document.get("pages")
    size = document.get("bytes", document.get("size_bytes"))
    zero_text_pages = document.get("zero_text_pages")
    scan_ratio = document.get("scan_ratio")
    if scan_ratio is None and isinstance(pages, int) and pages > 0 and isinstance(zero_text_pages, int):
        scan_ratio = zero_text_pages / pages
    layout = document.get("layout")
    return {
        "file": document.get("file", document.get("path")),
        "sha256": document.get("sha256"),
        "pages": pages,
        "size_bytes": size,
        "layout": layout if layout in SOURCE_LAYOUTS else "unknown",
        "zero_text_pages": zero_text_pages,
        "scan_ratio": scan_ratio,
        "mine": document.get("mine"),
        "mine_group": document.get("mine_group"),
        "split": document.get("split"),
        "registration_status": document.get("registration_status", "PENDING_SOURCE_REGISTRATION"),
        "registration_notes": document.get(
            "registration_notes", "需登记原文件 hash、页数、体积、扫描比例并由复核人确认。"
        ),
    }


def _empty_rule_record(field_key: str, source: str = "template") -> dict[str, Any]:
    return {
        "field_key": field_key,
        "answerability": None,
        "cardinality": None,
        "expected_facts": [],
        "absence_scope": None,
        "legacy_observations": [],
        "adjudication": {
            "status": "pending",
            "annotator": None,
            "adjudicator": None,
            "adjudicated_at": None,
            "source": source,
            "notes": "未完成原文独立标注和裁决；空 expected_facts 不表示缺失。",
        },
    }


def build_gold_matrix(
    documents: Iterable[Mapping[str, Any]],
    rules: Iterable[Mapping[str, Any]],
    *,
    dataset_id: str = "gas-report-gold-v2",
    source: str = "P01 template; pending human adjudication",
    legacy_truth: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a document-by-rule matrix without fabricating annotations."""

    normalized_rules = normalize_rule_catalog(rules)
    rule_keys = [str(rule["field_key"]) for rule in normalized_rules]
    if len(set(rule_keys)) != len(rule_keys):
        raise ValueError("rules contain duplicate field_key")
    old_by_doc: dict[str, list[dict[str, Any]]] = {}
    for row in (legacy_truth or {}).get("truth", []):
        old_by_doc.setdefault(str(row.get("doc")), []).append(copy.deepcopy(dict(row)))
    legacy_docs = (legacy_truth or {}).get("documents", {})
    matrix_docs = []
    for document in documents:
        document_id = str(document.get("document_id", document.get("id")))
        if document_id in {"None", ""}:
            raise ValueError("each document needs document_id or id")
        records = []
        for key in rule_keys:
            record = _empty_rule_record(key)
            for observation in old_by_doc.get(document_id, []):
                if observation.get("field_key") == key:
                    record["legacy_observations"].append(observation)
            records.append(record)
        legacy_doc = legacy_docs.get(document_id, {}) if isinstance(legacy_docs, Mapping) else {}
        source_doc = {**legacy_doc, **dict(document)}
        matrix_docs.append({
            "document_id": document_id,
            "label": source_doc.get("label", document_id),
            "source": _source_metadata(source_doc),
            "rules": records,
        })
    result = {
        "schema_version": SCHEMA_VERSION,
        "evaluation_contract_version": EVALUATION_CONTRACT_VERSION,
        "dataset_id": dataset_id,
        "source": source,
        "created_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "annotation_policy": {
            "expected_facts_are_complete": True,
            "legacy_values_are_observations_only": True,
            "empty_expected_facts_do_not_mean_absent": True,
            "human_adjudication_required_for_scoring": True,
        },
        "quality_thresholds": {
            "status": "PENDING_BUSINESS_APPROVAL",
            "note": "未填写业务最低召回、确定正确率、自动完整正确率和错误确定上限；不能据此宣称发布通过。",
            "metrics": {
                "minimum_fact_recall_by_key_rule": None,
                "minimum_fact_recall_by_stratum": None,
                "minimum_determinate_output_precision": None,
                "minimum_automatic_correct_completion_rate": None,
                "minimum_document_complete_correct_rate": None,
                "maximum_task_error_rate": None,
                "minimum_sample_size": None,
                "statistical_uncertainty_method": None,
            },
        },
        "rules": normalized_rules,
        "documents": matrix_docs,
    }
    errors = validate_gold_matrix(result)
    if errors:
        raise ValueError("generated invalid gold matrix: " + "; ".join(errors[:3]))
    return result


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _validate_evidence_list(value: Any, path: str, errors: list[str]) -> None:
    if not isinstance(value, list):
        errors.append(f"{path}: must be a list")
        return
    for index, evidence_id in enumerate(value):
        if not isinstance(evidence_id, str) or not evidence_id.strip():
            errors.append(f"{path}[{index}]: evidence id must be a non-empty string")


def _validate_fact(fact: Any, path: str, rule: Mapping[str, Any], errors: list[str]) -> None:
    if not isinstance(fact, Mapping):
        errors.append(f"{path}: must be an object")
        return
    required = {"fact_id", "value", "unit", "value_kind", "dimensions", "source_role",
                "attribute_evidence", "occurrences"}
    missing = required - set(fact)
    if missing:
        errors.append(f"{path}: missing {sorted(missing)}")
    if not isinstance(fact.get("fact_id"), str) or not fact.get("fact_id"):
        errors.append(f"{path}.fact_id: required non-empty string")
    if not isinstance(fact.get("dimensions"), Mapping):
        errors.append(f"{path}.dimensions: must be an object")
    else:
        allowed = set(rule.get("identity_dimensions", []))
        for key in fact["dimensions"]:
            if key not in allowed:
                errors.append(f"{path}.dimensions.{key}: not declared by rule")
    if not isinstance(fact.get("source_role"), str) or not fact.get("source_role"):
        errors.append(f"{path}.source_role: required non-empty string")
    attributes = fact.get("attribute_evidence")
    if not isinstance(attributes, Mapping):
        errors.append(f"{path}.attribute_evidence: must be an object")
    else:
        for attr, evidence in attributes.items():
            if attr == "dimensions":
                if not isinstance(evidence, Mapping):
                    errors.append(f"{path}.attribute_evidence.dimensions: must be an object")
                else:
                    for dim, ids in evidence.items():
                        _validate_evidence_list(ids, f"{path}.attribute_evidence.dimensions.{dim}", errors)
            else:
                _validate_evidence_list(evidence, f"{path}.attribute_evidence.{attr}", errors)
    registry: dict[str, Any] = {}
    for occurrence in fact.get("occurrences", []) if isinstance(fact.get("occurrences"), list) else []:
        if isinstance(occurrence, Mapping) and occurrence.get("occurrence_id"):
            registry[str(occurrence["occurrence_id"])] = occurrence
    extra_registry = fact.get("evidence_registry", {})
    if isinstance(extra_registry, Mapping):
        for evidence_id, evidence in extra_registry.items():
            if isinstance(evidence, Mapping):
                registry[str(evidence_id)] = evidence
    elif extra_registry is not None:
        errors.append(f"{path}.evidence_registry: must be an object when provided")
    if isinstance(attributes, Mapping):
        for attr, evidence_ids in attributes.items():
            values: list[Any]
            if attr == "dimensions" and isinstance(evidence_ids, Mapping):
                values = [item for ids in evidence_ids.values() if isinstance(ids, list) for item in ids]
            elif isinstance(evidence_ids, list):
                values = evidence_ids
            else:
                values = []
            for evidence_id in values:
                if isinstance(evidence_id, str) and evidence_id not in registry:
                    errors.append(f"{path}.attribute_evidence.{attr}: unknown evidence id {evidence_id}")
    occurrences = fact.get("occurrences")
    if not isinstance(occurrences, list) or not occurrences:
        errors.append(f"{path}.occurrences: at least one occurrence is required")
        return
    occurrence_ids: set[str] = set()
    for index, occurrence in enumerate(occurrences):
        opath = f"{path}.occurrences[{index}]"
        if not isinstance(occurrence, Mapping):
            errors.append(f"{opath}: must be an object")
            continue
        oid = occurrence.get("occurrence_id")
        if not isinstance(oid, str) or not oid or oid in occurrence_ids:
            errors.append(f"{opath}.occurrence_id: unique non-empty string required")
        occurrence_ids.add(oid)
        if (not isinstance(occurrence.get("page_number"), int)
                or isinstance(occurrence.get("page_number"), bool) or occurrence["page_number"] < 1):
            errors.append(f"{opath}.page_number: positive integer required")
        if occurrence.get("coordinate_system") != OCCURRENCE_COORDINATE_SYSTEM:
            errors.append(f"{opath}.coordinate_system: must be {OCCURRENCE_COORDINATE_SYSTEM}")
        bbox = occurrence.get("bbox")
        if not isinstance(bbox, list) or len(bbox) != 4 or not all(_is_number(v) and v >= 0 for v in bbox):
            errors.append(f"{opath}.bbox: four non-negative finite numbers required")
        elif bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
            errors.append(f"{opath}.bbox: positive-area [x0,y0,x1,y1] required")
        if not isinstance(occurrence.get("quote"), str) or not occurrence["quote"].strip():
            errors.append(f"{opath}.quote: non-empty source quote required")


def _validate_adjudicated(
    record: Mapping[str, Any], rule: Mapping[str, Any], source: Any,
    path: str, errors: list[str],
) -> None:
    adjudication = record["adjudication"]
    for name in ("annotator", "adjudicator", "adjudicated_at"):
        if not isinstance(adjudication.get(name), str) or not adjudication[name].strip():
            errors.append(f"{path}.adjudication.{name}: required for adjudicated records")
    if adjudication.get("annotator") == adjudication.get("adjudicator"):
        errors.append(f"{path}.adjudication: independent annotator/adjudicator required")
    if isinstance(adjudication.get("adjudicated_at"), str):
        try:
            parsed = datetime.fromisoformat(adjudication["adjudicated_at"])
            if parsed.tzinfo is None:
                raise ValueError("timezone required")
        except ValueError:
            errors.append(f"{path}.adjudication.adjudicated_at: ISO-8601 timestamp with timezone required")
    if record.get("answerability") not in ANSWERABILITY:
        errors.append(f"{path}.answerability: must be resolved before adjudication")
    if record.get("cardinality") != rule.get("cardinality"):
        errors.append(f"{path}.cardinality: must match frozen rule before adjudication")
    if not isinstance(source, Mapping):
        return
    if source.get("registration_status") != "REGISTERED":
        errors.append(f"{path}: source registration_status must be REGISTERED before adjudication")
    for name in ("sha256", "pages", "size_bytes"):
        if source.get(name) is None:
            errors.append(f"{path}: adjudication requires registered source {name}")
    for index, fact in enumerate(record.get("expected_facts", [])):
        if not isinstance(fact, Mapping):
            continue
        fpath = f"{path}.expected_facts[{index}]"
        if fact.get("value") is None or str(fact.get("value", "")).strip() == "":
            errors.append(f"{fpath}.value: cannot be unknown for a present fact")
        attributes = fact.get("attribute_evidence", {})
        if not isinstance(attributes, Mapping):
            continue
        required_attributes = {"value", "source_role"}
        for attr in ("unit", "value_kind", "table_relation"):
            if fact.get(attr) is not None:
                required_attributes.add(attr)
        required_attributes.update(rule.get("evidence_requirements", []))
        for attr in required_attributes:
            if not attributes.get(attr):
                errors.append(f"{fpath}.attribute_evidence.{attr}: required supporting evidence")
        dimensions = fact.get("dimensions", {})
        dim_evidence = attributes.get("dimensions", {})
        for dim in rule.get("applicable_dimensions", []):
            if not isinstance(dimensions, Mapping) or dimensions.get(dim) in (None, "", "unknown", "not_applicable"):
                errors.append(f"{fpath}.dimensions.{dim}: applicable dimension must be identified")
            if not isinstance(dim_evidence, Mapping) or not dim_evidence.get(dim):
                errors.append(f"{fpath}.attribute_evidence.dimensions.{dim}: supporting evidence required")
        pages = source.get("pages")
        if isinstance(pages, int):
            for occurrence in fact.get("occurrences", []):
                if isinstance(occurrence, Mapping) and isinstance(occurrence.get("page_number"), int):
                    if occurrence["page_number"] > pages:
                        errors.append(f"{fpath}.occurrences: page_number exceeds source pages")


def validate_gold_matrix(
    payload: Mapping[str, Any],
    *,
    require_adjudicated: bool = False,
) -> list[str]:
    """Return validation errors; an empty list means schema-valid."""

    errors: list[str] = []
    if not isinstance(payload, Mapping):
        return ["gold matrix must be an object"]
    if payload.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    if payload.get("evaluation_contract_version") != EVALUATION_CONTRACT_VERSION:
        errors.append(f"evaluation_contract_version must be {EVALUATION_CONTRACT_VERSION}")
    if not isinstance(payload.get("rules"), list) or not payload["rules"]:
        errors.append("rules must be a non-empty list")
    if not isinstance(payload.get("documents"), list) or not payload["documents"]:
        errors.append("documents must be a non-empty list")
    rules = payload.get("rules") if isinstance(payload.get("rules"), list) else []
    rule_keys: list[str] = []
    rule_by_key: dict[str, Mapping[str, Any]] = {}
    for i, rule in enumerate(rules):
        path = f"rules[{i}]"
        if not isinstance(rule, Mapping):
            errors.append(f"{path}: must be an object")
            continue
        key = rule.get("field_key")
        if not isinstance(key, str) or not key.strip():
            errors.append(f"{path}.field_key: required non-empty string")
            continue
        if key in rule_by_key:
            errors.append(f"{path}.field_key: duplicate {key}")
        rule_keys.append(key)
        rule_by_key[key] = rule
        if not isinstance(rule.get("rule_id"), str) or not rule["rule_id"]:
            errors.append(f"{path}.rule_id: required non-empty string")
        if not isinstance(rule.get("cardinality"), str) or rule.get("cardinality") not in CARDINALITIES:
            errors.append(f"{path}.cardinality: invalid value")
        if not isinstance(rule.get("scope"), str) or rule.get("scope") not in SCOPES:
            errors.append(f"{path}.scope: invalid value")
        for attr in ("identity_dimensions", "applicable_dimensions", "evidence_requirements"):
            if not isinstance(rule.get(attr), list) or not all(isinstance(v, str) and v for v in rule.get(attr, [])):
                errors.append(f"{path}.{attr}: must be a list of non-empty strings")
        if not isinstance(rule.get("rule_semantic_hash"), str) or not _SHA256.fullmatch(rule["rule_semantic_hash"]):
            errors.append(f"{path}.rule_semantic_hash: sha256 required")
    documents = payload.get("documents") if isinstance(payload.get("documents"), list) else []
    seen_docs: set[str] = set()
    for index, document in enumerate(documents):
        path = f"documents[{index}]"
        if not isinstance(document, Mapping):
            errors.append(f"{path}: must be an object")
            continue
        document_id = document.get("document_id")
        if not isinstance(document_id, str) or not document_id:
            errors.append(f"{path}.document_id: required non-empty string")
        elif document_id in seen_docs:
            errors.append(f"{path}.document_id: duplicate {document_id}")
        if isinstance(document_id, str):
            seen_docs.add(document_id)
        source = document.get("source")
        if not isinstance(source, Mapping):
            errors.append(f"{path}.source: required object")
        else:
            sha = source.get("sha256")
            if sha is not None and (not isinstance(sha, str) or not _SHA256.fullmatch(sha)):
                errors.append(f"{path}.source.sha256: null or lowercase sha256 required")
            pages = source.get("pages")
            if pages is not None and (not isinstance(pages, int) or isinstance(pages, bool) or pages < 1):
                errors.append(f"{path}.source.pages: null or positive integer required")
            size = source.get("size_bytes")
            if size is not None and (not isinstance(size, int) or isinstance(size, bool) or size < 0):
                errors.append(f"{path}.source.size_bytes: null or non-negative integer required")
            ratio = source.get("scan_ratio")
            if ratio is not None and (not _is_number(ratio) or not 0 <= ratio <= 1):
                errors.append(f"{path}.source.scan_ratio: null or number in [0,1] required")
        records = document.get("rules")
        if not isinstance(records, list):
            errors.append(f"{path}.rules: required list")
            continue
        seen_fields: set[str] = set()
        for j, record in enumerate(records):
            rpath = f"{path}.rules[{j}]"
            if not isinstance(record, Mapping):
                errors.append(f"{rpath}: must be an object")
                continue
            key = record.get("field_key")
            if not isinstance(key, str) or key not in rule_by_key:
                errors.append(f"{rpath}.field_key: not in frozen rules")
                continue
            if key in seen_fields:
                errors.append(f"{rpath}.field_key: duplicate {key}")
            seen_fields.add(key)
            rule = rule_by_key[key]
            if record.get("cardinality") not in (None, rule.get("cardinality")):
                errors.append(f"{rpath}.cardinality: differs from frozen rule")
            answerability = record.get("answerability")
            if answerability is not None and (not isinstance(answerability, str) or answerability not in ANSWERABILITY):
                errors.append(f"{rpath}.answerability: invalid value")
            adjudication = record.get("adjudication")
            if (not isinstance(adjudication, Mapping) or not isinstance(adjudication.get("status"), str)
                    or adjudication.get("status") not in ADJUDICATION_STATES):
                errors.append(f"{rpath}.adjudication.status: pending/draft/adjudicated required")
                adjudication = {"status": "pending"}
            adjudicated = adjudication.get("status") == "adjudicated"
            if require_adjudicated and not adjudicated:
                errors.append(f"{rpath}: adjudication is not complete")
            facts = record.get("expected_facts")
            if not isinstance(facts, list):
                errors.append(f"{rpath}.expected_facts: must be a list")
                facts = []
            fact_ids: set[str] = set()
            for k, fact in enumerate(facts):
                fact_path = f"{rpath}.expected_facts[{k}]"
                _validate_fact(fact, fact_path, rule, errors)
                if isinstance(fact, Mapping) and isinstance(fact.get("fact_id"), str) and fact.get("fact_id") in fact_ids:
                    errors.append(f"{fact_path}.fact_id: duplicate")
                if isinstance(fact, Mapping) and isinstance(fact.get("fact_id"), str):
                    fact_ids.add(fact.get("fact_id"))
            if adjudicated and answerability == "present" and not facts:
                errors.append(f"{rpath}: present requires complete expected_facts")
            if adjudicated and isinstance(answerability, str) and answerability in ANSWERABILITY - {"present"} and facts:
                errors.append(f"{rpath}: {answerability} cannot carry expected_facts")
            if answerability == "absent":
                scope = record.get("absence_scope")
                if adjudicated and not isinstance(scope, Mapping):
                    errors.append(f"{rpath}.absence_scope: required for adjudicated absent")
                if isinstance(scope, Mapping):
                    if scope.get("scope_type") not in {"whole_document", "pages", "attachments", "sections", "explicit_ranges"}:
                        errors.append(f"{rpath}.absence_scope.scope_type: invalid")
                    if not isinstance(scope.get("strategy"), str) or not scope["strategy"].strip():
                        errors.append(f"{rpath}.absence_scope.strategy: required")
            if answerability != "absent" and record.get("absence_scope") is not None:
                errors.append(f"{rpath}.absence_scope: only valid for absent")
            if adjudicated:
                _validate_adjudicated(record, rule, source, rpath, errors)
        if set(seen_fields) != set(rule_keys):
            errors.append(f"{path}.rules: must contain each frozen rule exactly once")
    return errors


def load_gold_matrix(path: str | Path, *, require_adjudicated: bool = False) -> dict[str, Any]:
    """Load a JSON matrix and raise ValueError with all schema errors."""

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    errors = validate_gold_matrix(payload, require_adjudicated=require_adjudicated)
    if errors:
        raise ValueError("Invalid gold matrix:\n- " + "\n- ".join(errors))
    return payload


def migrate_legacy_truth(
    legacy: Mapping[str, Any],
    rules: Iterable[Mapping[str, Any]],
    *,
    documents: Iterable[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Migrate the 2026-10-10 checklist to a pending matrix.

    Values from the old checklist are copied only into ``legacy_observations``.
    They never become expected facts or an ``absent`` assertion.
    """

    legacy_docs = legacy.get("documents", {})
    if documents is None:
        documents = [
            {"document_id": str(doc_id), **dict(meta)}
            for doc_id, meta in legacy_docs.items()
        ]
    return build_gold_matrix(
        documents,
        rules,
        dataset_id="gas-report-gold-v2-migrated",
        source="Migrated from truth_checklist_2026-10-10.json; pending human adjudication",
        legacy_truth=legacy,
    )


def _main() -> int:
    parser = argparse.ArgumentParser(description="Create or validate gold-matrix-v2 files")
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate")
    validate.add_argument("gold", type=Path)
    validate.add_argument("--require-adjudicated", action="store_true")
    migrate = sub.add_parser("migrate")
    migrate.add_argument("--truth", type=Path, required=True)
    migrate.add_argument("--rules", type=Path, required=True)
    migrate.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "validate":
        load_gold_matrix(args.gold, require_adjudicated=args.require_adjudicated)
        print(f"valid: {args.gold}")
        return 0
    legacy = json.loads(args.truth.read_text(encoding="utf-8"))
    raw_rules = json.loads(args.rules.read_text(encoding="utf-8"))
    rules = raw_rules.get("rules", raw_rules)
    result = migrate_legacy_truth(legacy, rules)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
