from __future__ import annotations

import json
from pathlib import Path

from scripts.eval.gold_matrix_v2 import (
    load_gold_matrix,
    migrate_legacy_truth,
    validate_gold_matrix,
)

ROOT = Path(__file__).resolve().parents[1]
EVAL = ROOT / "scripts" / "eval"


def test_migrated_template_is_document_rule_complete_and_pending():
    payload = load_gold_matrix(EVAL / "gold-v2-template-2026-10-12.json")
    assert payload["schema_version"] == "gold-matrix-v2"
    assert len(payload["documents"]) == 5
    assert len(payload["rules"]) == 10
    rule_keys = {rule["field_key"] for rule in payload["rules"]}
    for document in payload["documents"]:
        assert {item["field_key"] for item in document["rules"]} == rule_keys
        assert all(item["adjudication"]["status"] == "pending" for item in document["rules"])
        assert all(not item["expected_facts"] for item in document["rules"])
    assert not validate_gold_matrix(payload, require_adjudicated=False)


def test_require_adjudication_rejects_template():
    payload = load_gold_matrix(EVAL / "gold-v2-template-2026-10-12.json")
    errors = validate_gold_matrix(payload, require_adjudicated=True)
    assert errors
    assert any("adjudication is not complete" in error for error in errors)


def test_legacy_values_are_observations_only():
    legacy = json.loads((EVAL / "truth_checklist_2026-10-10.json").read_text(encoding="utf-8"))
    rules = json.loads((EVAL / "rules_checklist_2026-10-10.json").read_text(encoding="utf-8"))["rules"]
    payload = migrate_legacy_truth(legacy, rules)
    matching = [
        item for document in payload["documents"]
        for item in document["rules"]
        if item["legacy_observations"]
    ]
    assert matching
    assert all(item["adjudication"]["status"] == "pending" for item in matching)
    assert all(item["answerability"] is None for item in matching)
    assert all(not item["expected_facts"] for item in matching)


def test_missing_rule_breaks_fixed_denominator():
    payload = load_gold_matrix(EVAL / "gold-v2-template-2026-10-12.json")
    payload["documents"][0]["rules"].pop()
    assert any("each frozen rule" in error for error in validate_gold_matrix(payload))


def test_adjudicated_absence_requires_independent_registered_source_review():
    payload = load_gold_matrix(EVAL / "gold-v2-template-2026-10-12.json")
    document = payload["documents"][0]
    document["source"]["registration_status"] = "PENDING_SOURCE_REGISTRATION"
    record = document["rules"][0]
    record.update({
        "answerability": "absent", "cardinality": payload["rules"][0]["cardinality"],
        "absence_scope": {"scope_type": "whole_document", "strategy": "原图全范围人工复核"},
        "adjudication": {"status": "adjudicated", "annotator": "标注人甲",
                         "adjudicator": "复核人乙", "adjudicated_at": "2026-10-12T10:00:00+08:00"},
    })
    assert any("source registration_status" in error for error in validate_gold_matrix(payload))
    document["source"].update({"registration_status": "REGISTERED", "sha256": "a" * 64,
                               "pages": 22, "size_bytes": 1000})
    assert not validate_gold_matrix(payload)
    record["adjudication"]["adjudicator"] = record["adjudication"]["annotator"]
    assert any("independent annotator" in error for error in validate_gold_matrix(payload))


def test_malformed_answerability_is_rejected_without_crash():
    payload = load_gold_matrix(EVAL / "gold-v2-template-2026-10-12.json")
    payload["documents"][0]["rules"][0]["answerability"] = []
    assert any("answerability" in error for error in validate_gold_matrix(payload))

