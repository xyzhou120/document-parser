import json
import shutil
from pathlib import Path
from uuid import uuid4

import pytest

from scripts.eval.gold_matrix_v2 import semantic_hash
from scripts.reliable_evaluation_runner import adapt_run_export, run_evaluation


@pytest.fixture(name="tmp_path")
def workspace_tmp_path():
    # Python 3.12 on Windows applies restrictive ACLs to pytest's mode=0o700
    # directories. Use a uniquely owned workspace directory with inherited ACLs.
    root = Path(__file__).resolve().parents[2] / "output"
    path = root / f"p20-evaluation-test-{uuid4().hex}"
    path.mkdir(mode=0o777)
    try:
        yield path
    finally:
        assert path.resolve().parent == root.resolve()
        shutil.rmtree(path)


def _fact():
    location = {"occurrence_id": "o1", "page_number": 1,
                "bbox": [1, 1, 20, 20], "coordinate_system": "PDF_POINT", "quote": "2"}
    ids = ["o1"]
    return {"fact_id": "f1", "value": "2", "unit": "m3/t", "value_kind": "measured",
            "dimensions": {"workface": "40203"}, "source_role": "report_body",
            "table_relation": "value",
            "attribute_evidence": {"value": ids, "unit": ids, "value_kind": ids,
                                   "source_role": ids, "table_relation": ids,
                                   "dimensions": {"workface": ids}},
            "occurrences": [location]}


def _gold(tmp_path, *, approved=False):
    catalog = {"field_key": "gas", "rule_id": "gas", "rule_version": 1,
               "cardinality": "single", "scope": "whole_document",
               "identity_dimensions": ["workface"], "applicable_dimensions": ["workface"],
               "evidence_requirements": []}
    catalog["rule_semantic_hash"] = semantic_hash(catalog)
    fact = _fact()
    payload = {"schema_version": "gold-matrix-v2", "evaluation_contract_version": "gold-matrix-v2",
               "quality_thresholds": {"status": "APPROVED" if approved else "PENDING_BUSINESS_APPROVAL",
                                      "metrics": {"fact_recall": 1} if approved else {}},
               "rules": [catalog], "documents": [{"document_id": "d1", "source": {
                   "sha256": "a" * 64, "pages": 1, "size_bytes": 10, "layout": "native",
                   "registration_status": "REGISTERED"}, "rules": [{"field_key": "gas",
                   "answerability": "present", "cardinality": "single", "expected_facts": [fact],
                   "absence_scope": None, "adjudication": {"status": "adjudicated",
                   "annotator": "a", "adjudicator": "b", "adjudicated_at": "2026-10-12T00:00:00+00:00"}}]}]}
    path = tmp_path / "gold.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_runner_from_run_export_preserves_provenance_and_does_not_fill_missing_fields(tmp_path):
    _gold(tmp_path)
    export = {"run_id": "run-1", "file_hash": "a" * 64, "model": "fake", "model_version": "v1",
              "documents": [{"document_id": "d1", "fields": [{"field_key": "gas",
                  "semantic_state": "completed", "range_closed": True, "proof_valid": True,
                  "facts": [_fact()]}]}]}
    source = tmp_path / "run.json"
    source.write_text(json.dumps(export, ensure_ascii=False), encoding="utf-8")
    result = adapt_run_export(export)
    assert result["run_id"] == "run-1"
    assert result["documents"][0]["rules"][0]["facts"][0]["dimensions"]
    incomplete = adapt_run_export({"documents": [{"document_id": "d1", "fields": [{"field_key": "gas"}]}]})
    assert "facts" not in incomplete["documents"][0]["rules"][0]


def test_runner_marks_pending_gold_and_unapproved_thresholds_not_assessed(tmp_path):
    gold = _gold(tmp_path)
    predictions = tmp_path / "predictions.json"
    predictions.write_text(json.dumps({"documents": [{"document_id": "d1", "rules": []}]}), encoding="utf-8")
    out = tmp_path / "report.json"
    report = run_evaluation(gold_path=gold, predictions_path=predictions, output_path=out)
    assert report["release_assessment"]["status"] == "NOT_ASSESSED"
    assert "gold_thresholds_pending_business_approval" in report["release_assessment"]["reasons"]
    assert json.loads(out.read_text(encoding="utf-8"))["report_schema_version"] == "reliable-evaluation-report-v1"


def test_runner_can_assess_only_with_adjudicated_gold_and_approved_thresholds(tmp_path):
    gold = _gold(tmp_path, approved=True)
    predictions = tmp_path / "predictions.json"
    predictions.write_text(json.dumps({"documents": [{"document_id": "d1", "rules": [{
        "field_key": "gas", "semantic_state": "completed", "range_closed": True,
        "proof_valid": True, "facts": [_fact()]}]}]}), encoding="utf-8")
    report = run_evaluation(gold_path=gold, predictions_path=predictions, output_path=tmp_path / "r.json")
    assert report["release_assessment"]["status"] == "NOT_ASSESSED"
    assert report["release_assessment"]["readiness"] == "READY_FOR_QUALITY_REVIEW"


def test_runner_is_read_only_for_inputs_and_does_not_import_database(tmp_path):
    gold = _gold(tmp_path)
    predictions = tmp_path / "predictions.json"
    predictions.write_text(json.dumps({"documents": []}), encoding="utf-8")
    before = (gold.read_bytes(), predictions.read_bytes())
    run_evaluation(gold_path=gold, predictions_path=predictions, output_path=tmp_path / "r.json")
    assert (gold.read_bytes(), predictions.read_bytes()) == before


def test_p12_fact_view_and_execution_exports_keep_supported_receipts_without_gold():
    source = _fact()
    fact = {key: value for key, value in source.items() if key not in {"attribute_evidence", "occurrences"}}
    fact.update({"field_key": "gas", "available_for_downstream": True,
                 "source": "human_correction", "original_source": "human_corrected",
                 "evidence": [{**source["occurrences"][0], "evidence_id": "o1"}],
                 "assertions": {key: {"status": "supported", "evidence_ids": ["o1"], "value": value}
                                for key, value in {"value": "2", "unit": "m3/t", "value_kind": "measured",
                                                   "source_role": "report_body", "table_relation": "value",
                                                   "dimensions.workface": "40203"}.items()}})
    export = {"run_id": "run-a", "content_revision_id": "revision-a", "usage": {"total_tokens": 22},
              "fact_view": {"document_id": "d1", "contract_version": "reliable-extraction-v2", "facts": [fact]},
              "execution": {"jobs": [{"field_key": "gas", "attempt": 4, "rule_hash": "f" * 64,
                  "semantic_state": "completed", "checkpoint": {"field_completion": {
                      "range_closed": True, "proof_valid": True}}}]}}
    prediction = adapt_run_export(export)
    rule = prediction["documents"][0]["rules"][0]
    observed = rule["facts"][0]
    assert rule["semantic_state"] == "completed"
    assert rule["attempted"] is True
    assert rule["range_closed"] is True
    assert rule["rule_semantic_hash"] == "f" * 64
    assert observed["attribute_evidence"]["dimensions"] == {"workface": ["o1"]}
    assert observed["source"] == "human_correction"
    assert observed["original_source"] == "human_corrected"
    assert observed["occurrences"][0]["quote"] == "2"
    assert prediction["usage"] == {"total_tokens": 22}


def test_pending_adjudication_never_claims_release_readiness(tmp_path):
    gold_path = _gold(tmp_path, approved=True)
    gold = json.loads(gold_path.read_text(encoding="utf-8"))
    record = gold["documents"][0]["rules"][0]
    record["adjudication"]["status"] = "pending"
    gold_path.write_text(json.dumps(gold), encoding="utf-8")
    predictions = tmp_path / "p.json"
    predictions.write_text(json.dumps({"documents": []}), encoding="utf-8")
    report = run_evaluation(gold_path=gold_path, predictions_path=predictions, output_path=tmp_path / "r.json")
    assert report["release_assessment"]["status"] == "NOT_ASSESSED"
    assert report["release_assessment"]["readiness"] == "BLOCKED"
    assert "pending_gold_adjudication" in report["release_assessment"]["reasons"]


def test_report_cannot_overwrite_gold_or_prediction_inputs(tmp_path):
    gold = _gold(tmp_path)
    predictions = tmp_path / "p.json"
    predictions.write_text(json.dumps({"documents": []}), encoding="utf-8")
    for path in (gold, predictions):
        with pytest.raises(ValueError, match="must not overwrite"):
            run_evaluation(gold_path=gold, predictions_path=predictions, output_path=path)
