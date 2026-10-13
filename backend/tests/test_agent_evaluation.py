import copy

import pytest

from scripts.agent_evaluation import (
    annotation_template,
    evaluate,
    import_results,
    manual_cost_report,
    normalize_value,
    select_samples,
    stability,
    validate_annotations,
)


def inventory_row(doc_id, mine, hash_char, zero=0, pages=2):
    return {
        "id": doc_id, "mine": mine, "sha256": hash_char * 64, "status": "pdf",
        "path": f"{mine}/{doc_id}.pdf", "pages": pages, "zero_text_pages": zero,
        "ocr_gate_pages": zero, "bytes": 100,
    }


def fact(value="2.01", unit="m³/t", workface="40203"):
    return {
        "raw_value": value, "normalized_value": value, "unit": unit,
        "dimensions": {"workface": workface}, "value_kind": "measured",
        "source_role": "report_body", "evidence": [{"page_number": 1,
            "bbox": [10, 10, 30, 20], "coordinate_system": "PDF_POINT", "role": "value"}],
    }


def gold_field(facts=None, status="VERIFIED"):
    return {"field_key": "gas_content", "status": status, "scope": "whole_document",
            "scope_pages": [], "complete": True, "annotator": "甲", "adjudicator": "乙",
            "verified_at": "2026-10-08", "facts": [fact()] if facts is None else facts}


def dataset(field):
    return {"documents": [{"id": 1, "sha256": "a" * 64, "pages": 2, "fields": [field]}]}


def prediction(facts):
    return dataset({"field_key": "gas_content", "facts": facts})


def test_selection_dedups_files_and_keeps_duplicate_connected_mines_together():
    rows = [inventory_row(10, "甲", "a"), inventory_row(11, "乙", "a"),
            inventory_row(12, "乙", "b"), inventory_row(13, "丙", "c", 2)]
    selected = select_samples(rows, 4)
    docs = selected["documents"]
    assert len(docs) == 3
    assert len({d["sha256"] for d in docs}) == 3
    assert len({d["split"] for d in docs if d["mine"] in {"甲", "乙"}}) == 1
    assert selected == select_samples(list(reversed(rows)), 4)
    assert {d["layout"] for d in docs} == {"native", "scan"}


def test_templates_never_label_ground_truth_or_report_perfect_empty_score():
    manifest = select_samples([inventory_row(10, "甲", "a"), inventory_row(11, "乙", "b")], 2)
    gold = annotation_template(manifest, ["gas_content"])
    assert not validate_annotations(gold)
    result = evaluate(gold, {"documents": []})
    assert result["status"] == "NO_VERIFIED_GOLD"
    assert result["summary"]["precision"] is None
    assert result["summary"]["recall"] is None


def test_exact_normalization_groups_same_value_without_losing_evidence():
    p = fact("2.010")
    p2 = fact("2.01")
    p2["evidence"][0]["page_number"] = 2
    result = evaluate(dataset(gold_field()), prediction([p, p2]), 0.5)
    assert result["summary"]["tp"] == 1
    assert result["summary"]["fp"] == 0
    assert result["summary"]["evidence_support_rate"] == 1


@pytest.mark.parametrize("wrong", [fact(unit="m²/t"), fact(workface="40204"), fact(value="2.014")])
def test_wrong_unit_binding_or_rounded_value_is_fp_and_fn(wrong):
    result = evaluate(dataset(gold_field()), prediction([wrong]))
    assert result["summary"]["tp"] == 0
    assert result["summary"]["fp"] == 1
    assert result["summary"]["fn"] == 1
    assert result["summary"]["precision"] == 0
    assert result["summary"]["recall"] == 0


def test_absent_is_scoreable_only_after_complete_independent_review():
    gold = dataset(gold_field([], "ABSENT"))
    result = evaluate(gold, prediction([fact()]))
    assert result["summary"]["fp"] == 1
    assert result["summary"]["recall"] is None
    gold["documents"][0]["fields"][0]["adjudicator"] = "甲"
    with pytest.raises(ValueError, match="independent"):
        evaluate(gold, prediction([]))


def test_missing_prediction_is_false_negative_and_hash_mismatch_is_rejected():
    gold = dataset(gold_field())
    result = evaluate(gold, {"documents": []})
    assert result["summary"]["fn"] == 1
    p = prediction([fact()])
    p["documents"][0]["sha256"] = "b" * 64
    with pytest.raises(ValueError, match="hash mismatch"):
        evaluate(gold, p)


def test_evidence_does_not_count_wrong_page_or_other_coordinate_system():
    p = fact()
    p["evidence"][0]["page_number"] = 2
    result = evaluate(dataset(gold_field()), prediction([p]), 0.5)
    assert result["summary"]["tp"] == 1
    assert result["summary"]["evidence_supported_tp"] == 0
    p["evidence"][0]["page_number"] = 1
    p["evidence"][0]["coordinate_system"] = "PIXEL"
    assert evaluate(dataset(gold_field()), prediction([p]), 0.5)["summary"]["supported_precision"] == 0


def test_all_required_evidence_roles_must_be_supported():
    g = fact()
    g["evidence"].append({**g["evidence"][0], "role": "unit"})
    assert evaluate(dataset(gold_field([g])), prediction([fact()]))["summary"]["evidence_supported_tp"] == 0


def test_sampled_scope_does_not_claim_whole_document_recall():
    field = gold_field()
    field.update(scope="sampled_pages", scope_pages=[1])
    outside = fact("99")
    outside["evidence"][0]["page_number"] = 2
    result = evaluate(dataset(field), prediction([fact(), outside]))
    assert result["summary"]["sampled_scope_fields"] == 1
    assert result["summary"]["outside_scope_predictions"] == 1
    assert result["summary"]["fp"] == 0


def test_stability_tracks_value_changes_but_never_claims_accuracy():
    p = prediction([fact()])
    changed = copy.deepcopy(p)
    changed["documents"][0]["fields"][0]["facts"][0]["normalized_value"] = "99"
    assert stability([p, p])["stable_fraction"] == 1
    assert stability([p, changed])["stable_fraction"] == 0
    assert stability([p])["stable_fraction"] is None


def test_normalization_never_guesses_comma_or_numeric_ocr_error():
    assert normalize_value("2.010") == normalize_value("2.01")
    assert normalize_value("2.014") != normalize_value("2.01")
    assert normalize_value("1,000") == normalize_value("1000")
    assert normalize_value("1,2") != normalize_value("12")
    assert normalize_value("2.O1") != normalize_value("2.01")
    assert normalize_value("-0") == normalize_value("0")


def test_import_requires_file_hash_and_supports_agent_value_groups():
    manifest = {"documents": [{"id": 1, "sha256": "a" * 64, "pages": 2}]}
    payload = {"file_hash": "a" * 64, "details": [{"field_key": "gas_content",
        "value_groups": [{"value": "2.01", "unit": "m³/t", "identity": {"workface": "40203"},
          "value_kind": "measured", "evidence_ids": ["e1"]}],
        "evidence_registry": {"e1": {"page_number": 1, "bbox": {"x0": 1, "y0": 1, "x1": 4, "y1": 4},
                                        "coordinate_system": "PDF_POINT"}}}]}
    result = import_results(manifest, 1, payload)
    group = result["documents"][0]["fields"][0]["facts"][0]
    assert group["dimensions"] == {"workface": "40203"}
    assert group["evidence"][0]["bbox"] == [1, 1, 4, 4]
    payload["file_hash"] = "b" * 64
    with pytest.raises(ValueError, match="file_hash"):
        import_results(manifest, 1, payload)


def test_import_agent_roles_keep_multi_role_header_and_dimension_only_references():
    manifest = {"documents": [{"id": 1, "sha256": "a" * 64, "pages": 2}]}
    registry = {
        name: {"page_number": 1, "bbox": [10, 10, 30, 20], "coordinate_system": "PDF_POINT"}
        for name in ("value_ref", "header_ref", "workface_ref", "unknown_ref")
    }
    payload = {"file_hash": "a" * 64, "fields": [{"field_key": "gas_content",
        "extraction_detail": {"instances": [{"value": "2.01", "unit": "m³/t",
            "value_kind": "measured", "source_role": "report_body",
            "evidence_ids": ["value_ref"],
            "dimensions": {"workface": {"value": "40203", "evidence_ids": ["workface_ref", "unknown_ref"]}},
            "evidence_roles": {"value_ref": ["value", "unit"],
                               "header_ref": ["column_header"],
                               "workface_ref": ["scope", "object"]}}],
            "evidence_registry": registry}}]}
    imported = import_results(manifest, 1, payload)
    evidence = imported["documents"][0]["fields"][0]["facts"][0]["evidence"]
    assert {e["role"] for e in evidence} == {"value", "unit", "column_header", "scope", "object", "unknown"}
    assert len(evidence) == 6
    gold = fact()
    gold["evidence"] = [{**gold["evidence"][0], "role": r}
                        for r in ("value", "unit", "column_header", "scope", "object")]
    score = evaluate(dataset(gold_field([gold])), imported, evidence_iou=0.5)
    assert score["summary"]["evidence_supported_tp"] == 1
    assert payload["fields"][0]["extraction_detail"]["evidence_registry"] == registry


def test_import_dimension_references_without_roles_stay_unknown():
    manifest = {"documents": [{"id": 1, "sha256": "a" * 64, "pages": 2}]}
    payload = {"file_hash": "a" * 64, "details": [{"field_key": "gas_content",
        "instances": [{"value": "2.01", "dimensions": {"workface": {"value": "40203", "evidence_ids": ["only_dim"]}},
                       "evidence_ids": []}],
        "evidence_registry": {"only_dim": {"page_number": 1, "bbox": [0, 0, 10, 10],
                                             "coordinate_system": "PDF_POINT"}}}]}
    result = import_results(manifest, 1, payload)
    evidence = result["documents"][0]["fields"][0]["facts"][0]["evidence"]
    assert len(evidence) == 1
    assert evidence[0]["role"] == "unknown"


def test_verified_evidence_requires_finite_in_bounds_pdf_points_and_known_role():
    gold = dataset(gold_field())
    evidence = gold["documents"][0]["fields"][0]["facts"][0]["evidence"][0]
    evidence["bbox"] = [10, 10, float("nan"), 20]
    assert any("finite" in e for e in validate_annotations(gold))
    evidence["bbox"] = [10, 10, 30, 20]
    evidence["role"] = "not-a-role"
    assert any("known explicit role" in e for e in validate_annotations(gold))
    evidence["role"] = "value"
    evidence["coordinate_system"] = "PIXEL"
    assert any("PDF_POINT" in e for e in validate_annotations(gold))


def test_manual_cost_report_uses_active_work_and_separates_phases():
    report = manual_cost_report([
        {"phase": "annotation", "document_id": 1, "active_seconds": 120, "facts": 4},
        {"phase": "review", "document_id": 1, "active_seconds": 60, "facts": 2,
         "reviewed_facts": 2, "corrected_facts": 1, "rejected_facts": 0},
    ])
    assert report["status"] == "RECORDED"
    review = next(row for row in report["phases"] if row["phase"] == "review")
    assert review["correction_rate"] == 0.5


def test_evaluation_stratifies_by_layout_and_keeps_scope_warnings():
    gold = dataset(gold_field())
    gold["documents"][0].update({"split": "dev", "layout": "scan", "mine_group": "甲", "pages": 2})
    result = evaluate(gold, prediction([fact()]))
    assert any(row["dimension"] == "layout" and row["value"] == "scan" for row in result["stratified"])
    assert result["scope_warnings"]


@pytest.mark.parametrize("bbox", [[0, 0, 301, 20], [-1, 0, 10, 20], [10, 0, 10, 20], [0, 0, float("inf"), 20], [False, 0, 10, 20]])
def test_formal_annotation_rejects_bad_geometry(bbox):
    gold = dataset(gold_field())
    gold["documents"][0]["page_bounds"] = [{"page_number": 1, "width": 300, "height": 300}]
    gold["documents"][0]["fields"][0]["facts"][0]["evidence"][0]["bbox"] = bbox
    assert validate_annotations(gold)


def test_strata_count_unique_documents_and_missing_predictions_as_false_negatives():
    gold = dataset(gold_field())
    gold["documents"][0]["fields"].append({**gold_field(), "field_key": "other"})
    result = evaluate(gold, {"documents": []})
    assert result["summary"]["fn"] == 2
    assert result["document_outcomes"]["status_counts"] == {"MISSING": 1}
    split = next(row for row in result["stratified"] if row["dimension"] == "split")
    assert split["documents"] == 1
    assert split["fields"] == 2
    assert split["fn"] == 2


def test_stability_rejects_version_changes_and_tracks_evidence_separately():
    p = prediction([fact()])
    p["run_metadata"] = {"frozen_configuration": {"knowledge_hash": "v1"}}
    altered = copy.deepcopy(p)
    altered["run_metadata"]["frozen_configuration"]["knowledge_hash"] = "v2"
    with pytest.raises(ValueError, match="configuration mismatch"):
        stability([p, altered])
    altered = copy.deepcopy(p)
    altered["documents"][0]["fields"][0]["facts"][0]["evidence"][0]["bbox"] = [1, 1, 2, 2]
    result = stability([p, altered, p])
    assert result["stable_fraction"] == 1
    assert result["changed_evidence_tasks"]
    assert result["three_run_complete"] is True
    assert result["configuration_status"] == "FROZEN"


def test_invalid_manual_cost_does_not_silently_enter_report():
    with pytest.raises(ValueError, match="active_seconds"):
        manual_cost_report([{"document_id": 1, "phase": "review", "active_seconds": float("nan")}])
    with pytest.raises(ValueError, match="dispositions"):
        manual_cost_report([{"document_id": 1, "phase": "review", "active_seconds": 60,
                             "reviewed_facts": 1, "corrected_facts": 2}])
