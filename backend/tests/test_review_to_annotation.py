from unittest.mock import patch

import pytest

from scripts.agent_evaluation import evaluate, validate_annotations
from scripts.review_to_annotation import bridge


def manifest():
    return {"checklist_type": "gas_report_agent", "documents": [{
        "id": 131, "sha256": "a" * 64, "path": "sample.pdf", "pages": 1,
        "split": "dev", "sampling_notes": "scan",
    }]}


def review(action="CORRECT", revision=1, file_hash="a" * 64, facts=None):
    return {"document_id": 131, "field_key": "actual_hole_count", "action": action,
            "revision": revision, "reviewer": "甲", "reason": "原图核对",
            "file_hash": file_hash, "facts": facts if facts is not None else [{
                "raw_value": "321", "value": "321", "unit": "个", "value_kind": "actual",
                "source_role": "evaluation_form", "dimensions": {"workface": "40307"},
                "evidence": [{"page_number": 1, "bbox": [10, 10, 30, 20],
                              "coordinate_system": "PDF_POINT", "role": "value"}],
            }]}


def test_bridge_creates_draft_and_keeps_provenance():
    with patch("scripts.review_to_annotation.read_json", return_value=review()):
        result = bridge(manifest(), ["review.json"])
    field = next(f for f in result["documents"][0]["fields"] if f["field_key"] == "actual_hole_count")
    assert result["annotation_status"] == "DRAFT"
    assert field["status"] == "DRAFT"
    assert field["complete"] is False
    assert field["adjudicator"] == ""
    assert field["facts"][0]["normalized_value"] == "321"
    assert field["review_provenance"]["revision"] == 1
    assert field["uncertainty"]


def test_bridge_rejects_hash_mismatch():
    with patch("scripts.review_to_annotation.read_json", return_value=review(file_hash="b" * 64)):
        with pytest.raises(ValueError, match="hash mismatch"):
            bridge(manifest(), ["review.json"])


def test_bridge_reject_does_not_become_absent():
    with patch("scripts.review_to_annotation.read_json", return_value=review(action="REJECT", facts=[])):
        result = bridge(manifest(), ["review.json"])
    field = next(f for f in result["documents"][0]["fields"] if f["field_key"] == "actual_hole_count")
    assert field["status"] == "DRAFT"
    assert field["facts"] == []
    assert "不等于原文不存在" in field["notes"]


def test_bridge_requires_positive_revision():
    with patch("scripts.review_to_annotation.read_json", return_value=review(revision=0)):
        with pytest.raises(ValueError, match="positive revision"):
            bridge(manifest(), ["review.json"])


def test_bridge_does_not_replace_higher_revision_with_older_one():
    with patch("scripts.review_to_annotation.read_json", side_effect=[review(revision=2), review(revision=1, facts=[])]):
        result = bridge(manifest(), ["first.json", "second.json"])
    field = next(f for f in result["documents"][0]["fields"] if f["field_key"] == "actual_hole_count")
    assert field["review_provenance"]["revision"] == 2
    assert field["facts"][0]["normalized_value"] == "321"


def test_actual_api_export_uuid_document_matches_by_hash_and_is_not_scored():
    row = review()
    del row["document_id"]
    export = {"parse_run_id": "run-uuid", "document_id": "document-uuid",
              "source": "human_review_revisions", "facts": [row]}
    with patch("scripts.review_to_annotation.read_json", return_value=export):
        result = bridge(manifest(), ["export.json"])
    assert not validate_annotations(result)
    metrics = evaluate(result, {"documents": []})
    assert metrics["status"] == "NO_VERIFIED_GOLD"
    assert metrics["summary"]["precision"] is None
    assert metrics["summary"]["recall"] is None


def test_page_only_evidence_kept_pending_until_bbox_is_annotated():
    row = review()
    row["facts"][0]["evidence"][0]["bbox"] = None
    with patch("scripts.review_to_annotation.read_json", return_value=row):
        result = bridge(manifest(), ["review.json"])
    field = next(f for f in result["documents"][0]["fields"] if f["field_key"] == "actual_hole_count")
    assert field["facts"][0]["evidence"] == []
    assert field["pending_evidence"][0]["page_number"] == 1
    assert not validate_annotations(result)
