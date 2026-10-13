from copy import deepcopy

import pytest

from scripts.eval.gold_matrix_v2 import semantic_hash
from scripts.evaluation_v2 import evaluate


def _fact(value="2", *, page=1, occurrence_id="o1", kind="measured", unit="m3/t"):
    return {
        "fact_id": f"f-{occurrence_id}", "value": value, "unit": unit,
        "value_kind": kind, "dimensions": {"workface": "40203"},
        "source_role": "report_body", "table_relation": "value",
        "attribute_evidence": {name: [occurrence_id] for name in
                               ("value", "unit", "value_kind", "source_role", "table_relation")},
        "occurrences": [{"occurrence_id": occurrence_id, "page_number": page,
                         "quote": f"值 {value}", "bbox": [10, 10, 50, 30],
                         "coordinate_system": "PDF_POINT"}],
    }


def _supported_fact(*args, **kwargs):
    result = _fact(*args, **kwargs)
    result["attribute_evidence"]["dimensions"] = {"workface": [result["occurrences"][0]["occurrence_id"]]}
    return result


def _gold(answerability="present", facts=None, *, field_key="gas", cardinality="single",
          adjudication="adjudicated"):
    return {"field_key": field_key, "answerability": answerability,
            "cardinality": cardinality, "expected_facts": facts or [],
            "absence_scope": {"scope_type": "whole_document", "strategy": "full review"}
                             if answerability == "absent" else None,
            "adjudication": {"status": adjudication, "annotator": "a", "adjudicator": "b",
                             "adjudicated_at": "2026-10-12T00:00:00+00:00"}}


def _payload(rule, *, doc="d1"):
    catalog = {"field_key": rule["field_key"], "rule_id": rule["field_key"], "rule_version": 1,
               "cardinality": rule.get("cardinality", "single"), "scope": "whole_document",
               "identity_dimensions": ["workface"], "applicable_dimensions": ["workface"],
               "evidence_requirements": []}
    catalog["rule_semantic_hash"] = semantic_hash(catalog)
    for fact in rule.get("expected_facts", []):
        fact["attribute_evidence"]["dimensions"] = {"workface": [fact["occurrences"][0]["occurrence_id"]]}
    return {"schema_version": "gold-matrix-v2", "evaluation_contract_version": "gold-matrix-v2",
            "rules": [catalog], "documents": [{"document_id": doc, "source": {
                "sha256": "a" * 64, "pages": 3, "size_bytes": 2000,
                "layout": "native", "registration_status": "REGISTERED",
            }, "rules": [rule]}]}


def _prediction(rule, *, doc="d1", state="completed", **extra):
    return {"documents": [{"document_id": doc, "rules": [
        {"field_key": rule["field_key"], "semantic_state": state,
         "range_closed": True, "proof_valid": True, "job_created": True, "attempted": True, **extra}
    ]}]}


def test_document_rule_fixed_denominator_and_exact_fact_match():
    expected = _fact()
    gold = _gold(facts=[expected])
    result = evaluate(_payload(gold), _prediction(gold, facts=[_supported_fact()]))
    assert result["summary"]["document_rule_tasks"] == 1
    assert result["summary"]["tp"] == 1
    assert result["summary"]["automatic_correct_tasks"] == 1
    assert result["summary"]["fact_recall"] == 1


def test_wrong_object_or_unit_is_fp_and_fn():
    expected = _fact()
    gold = _gold(facts=[expected])
    wrong = _supported_fact(unit="kg")
    wrong["dimensions"] = {"workface": "other"}
    result = evaluate(_payload(gold), _prediction(gold, facts=[wrong]))
    assert result["summary"]["tp"] == 0
    assert result["summary"]["fp"] == 1
    assert result["summary"]["fn"] == 1
    assert result["summary"]["determinate_output_precision"] == 0
    assert result["summary"]["task_error_rate"] == 1


def test_duplicate_fact_is_reported_and_blocks_automatic_completion():
    expected = _supported_fact()
    gold = _gold(facts=[expected])
    duplicate = _supported_fact()
    result = evaluate(_payload(gold), _prediction(gold, facts=[expected, duplicate]))
    assert result["summary"]["tp"] == 1
    assert result["summary"]["duplicate_outputs"] == 1
    assert result["summary"]["automatic_correct_tasks"] == 0
    assert result["summary"]["task_error_tasks"] == 1


def test_absent_requires_not_found_closed_valid_proof_and_no_fact():
    gold = _gold(answerability="absent")
    good = evaluate(_payload(gold), _prediction(gold, state="not_found"))
    assert good["summary"]["automatic_correct_tasks"] == 1
    pending = _prediction(gold, state="unknown")
    result = evaluate(_payload(gold), pending)
    assert result["summary"]["automatic_correct_tasks"] == 0


def test_pending_adjudication_is_reported_and_stays_out_of_formal_denominator():
    gold = _gold(facts=[_fact()], adjudication="pending")
    result = evaluate(_payload(gold), _prediction(gold, facts=[_supported_fact()]))
    assert result["summary"]["document_rule_tasks"] == 1
    assert result["summary"]["pending_adjudication_tasks"] == 1
    assert result["summary"]["adjudicated_present_absent_tasks"] == 0
    assert result["summary"]["fact_recall"] is None


def test_review_and_incomplete_are_not_automatic_success():
    expected = _fact()
    gold = _gold(facts=[expected])
    result = evaluate(_payload(gold), _prediction(gold, state="review_required", facts=[expected]))
    assert result["summary"]["automatic_correct_tasks"] == 0
    result = evaluate(_payload(gold), _prediction(gold, state="incomplete", facts=[]))
    assert result["summary"]["automatic_correct_tasks"] == 0


def test_manual_fact_does_not_count_as_automatic_extraction():
    expected = _fact()
    gold = _gold(facts=[expected])
    fact = _fact()
    fact["provenance"] = "human"
    result = evaluate(_payload(gold), _prediction(gold, facts=[fact]))
    assert result["summary"]["tp"] == 0
    assert result["summary"]["manual_output_tasks"] == 1
    assert result["summary"]["automatic_correct_tasks"] == 0
    assert result["summary"]["determinate_output_precision"] is None


@pytest.mark.parametrize("provenance", [
    {"source": "human_correction"},
    {"original_source": "human_correction"},
    {"source_kind": "human_corrected"},
    {"provenance": {"original_source": "human_corrected"}},
])
def test_human_corrected_p10_facts_never_count_as_original_automatic_extraction(provenance):
    gold = _gold(facts=[_supported_fact()])
    fact = {**_supported_fact(), **provenance}
    result = evaluate(_payload(gold), _prediction(gold, facts=[fact]))
    assert result["summary"]["manual_facts_excluded"] == 1
    assert result["rows"][0]["manual_facts_excluded"] == 1
    assert result["summary"]["automatic_correct_tasks"] == 0
    assert result["summary"]["tp"] == 0


def test_automatic_ocr_source_is_still_automatic():
    gold = _gold(facts=[_supported_fact()])
    fact = {**_supported_fact(), "source": "automatic_ocr", "original_source": "automatic_ocr"}
    result = evaluate(_payload(gold), _prediction(gold, facts=[fact]))
    assert result["summary"]["manual_facts_excluded"] == 0
    assert result["summary"]["automatic_correct_tasks"] == 1


def test_all_occurrences_requires_complete_location_set():
    first, second = _fact(occurrence_id="o1"), _fact("3", occurrence_id="o2", page=2)
    gold = _gold(facts=[first, second], cardinality="all_occurrences")
    result = evaluate(_payload(gold), _prediction(gold, facts=[first]))
    assert result["summary"]["tp"] == 1
    assert result["summary"]["automatic_correct_tasks"] == 0


def test_one_fact_three_locations_cannot_pass_with_one_location():
    expected = _supported_fact()
    expected["occurrences"].extend([
        {**expected["occurrences"][0], "occurrence_id": "o2", "page_number": 2},
        {**expected["occurrences"][0], "occurrence_id": "o3", "page_number": 3},
    ])
    gold = _gold(facts=[expected], cardinality="all_occurrences")
    result = evaluate(_payload(gold), _prediction(gold, facts=[_supported_fact()]))
    assert result["summary"]["tp"] == 1
    assert result["summary"]["automatic_correct_tasks"] == 0
    result = evaluate(_payload(gold), _prediction(gold, facts=[expected]))
    assert result["summary"]["automatic_correct_tasks"] == 1


def test_all_occurrences_extra_position_is_wrong_determined_output():
    expected = _supported_fact()
    observed = deepcopy(expected)
    observed["occurrences"].append({**expected["occurrences"][0], "occurrence_id": "o2", "page_number": 2})
    gold = _gold(facts=[expected], cardinality="all_occurrences")
    result = evaluate(_payload(gold), _prediction(gold, facts=[observed]))
    assert result["summary"]["tp"] == 1
    assert result["summary"]["automatic_correct_tasks"] == 0
    assert result["summary"]["wrong_occurrences"] == 1
    assert result["summary"]["task_error_rate"] == 1


def test_same_numeric_value_misclassified_calculated_is_wrong():
    gold = _gold(facts=[_supported_fact("0.18")])
    result = evaluate(_payload(gold), _prediction(gold, facts=[_supported_fact("0.18", kind="calculated")]))
    assert result["summary"]["tp"] == 0
    assert result["summary"]["fp"] == 1
    assert result["summary"]["fn"] == 1


def test_copied_evidence_id_with_wrong_page_does_not_support_fact():
    expected = _supported_fact()
    observed = _supported_fact(page=2)
    gold = _gold(facts=[expected])
    result = evaluate(_payload(gold), _prediction(gold, facts=[observed]))
    assert result["summary"]["tp"] == 0
    assert result["summary"]["fp"] == 1


def test_missing_rule_cannot_shrink_matrix_or_document_denominator():
    first = _gold(facts=[_supported_fact()])
    gold = _payload(first)
    second = deepcopy(first)
    second["field_key"] = "other"
    gold["documents"][0]["rules"].append(second)
    catalog = deepcopy(gold["rules"][0])
    catalog.update(field_key="other", rule_id="other")
    catalog["rule_semantic_hash"] = semantic_hash(catalog)
    gold["rules"].append(catalog)
    result = evaluate(gold, _prediction(first, facts=[_supported_fact()]))
    assert result["summary"]["document_rule_tasks"] == 2
    assert result["summary"]["automatic_correct_completion_rate"] == 0.5
    assert result["summary"]["document_complete_correct_rate"] == 0
    assert result["summary"]["planned_coverage_rate"] == 0.5
    assert result["summary"]["execution_record_rate"] == 0.5
    assert result["summary"]["range_closed_rate"] == 0.5


def test_all_not_found_is_error_for_present_and_precision_is_undefined():
    gold = _gold(facts=[_supported_fact()])
    result = evaluate(_payload(gold), _prediction(gold, state="not_found"))
    assert result["summary"]["determinate_output_precision"] is None
    assert result["summary"]["task_error_rate"] == 1
    assert result["summary"]["not_found_error_rate"] == 1
    assert result["summary"]["automatic_correct_completion_rate"] == 0


@pytest.mark.parametrize("extra", [{"range_closed": False}, {"proof_valid": False}, {"facts": [_supported_fact()]}])
def test_absent_does_not_pass_without_closed_proven_empty_scope(extra):
    gold = _gold(answerability="absent")
    result = evaluate(_payload(gold), _prediction(gold, state="not_found", **extra))
    assert result["summary"]["automatic_correct_tasks"] == 0


def test_pending_gold_cannot_be_reported_as_evaluated_and_strict_mode_rejects_it():
    gold = _payload(_gold(adjudication="pending"))
    result = evaluate(gold, {"documents": []})
    assert result["status"] == "NO_ADJUDICATED_GOLD"
    assert result["summary"]["evaluable_documents"] == 0
    with pytest.raises(ValueError, match="adjudication"):
        evaluate(gold, {"documents": []}, require_adjudicated=True)


def test_wrong_file_and_rule_snapshot_are_rejected():
    gold = _gold(facts=[_supported_fact()])
    payload = _payload(gold)
    predictions = _prediction(gold, facts=[_supported_fact()])
    predictions["documents"][0]["sha256"] = "b" * 64
    with pytest.raises(ValueError, match="hash mismatch"):
        evaluate(payload, predictions)
    predictions = _prediction(gold, facts=[_supported_fact()], rule_semantic_hash="b" * 64)
    with pytest.raises(ValueError, match="snapshot mismatch"):
        evaluate(payload, predictions)


def test_invalidated_source_and_missing_attribute_evidence_are_wrong():
    gold = _gold(facts=[_supported_fact()])
    invalidated = _supported_fact()
    invalidated["source_validity"] = "invalidated"
    result = evaluate(_payload(gold), _prediction(gold, facts=[invalidated]))
    assert result["summary"]["tp"] == 0
    unsupported = _supported_fact()
    unsupported["attribute_evidence"].pop("unit")
    result = evaluate(_payload(gold), _prediction(gold, facts=[unsupported]))
    assert result["summary"]["tp"] == 0


def test_frozen_explicit_unit_conversion_is_applied_without_implicit_conversion():
    expected = _supported_fact("2", unit="m3/t")
    observed = _supported_fact("2000", unit="L/t")
    # 来源引文和物理证据来自同一个转换后的事实。
    observed["occurrences"][0]["quote"] = expected["occurrences"][0]["quote"]
    gold = _gold(facts=[expected])
    payload = _payload(gold)
    result = evaluate(payload, _prediction(gold, facts=[observed]))
    assert result["summary"]["tp"] == 0
    payload["rules"][0]["unit_policy"] = {"conversions": [{"from": "L/t", "to": "m3/t", "factor": "0.001"}]}
    payload["rules"][0]["rule_semantic_hash"] = semantic_hash(payload["rules"][0])
    result = evaluate(payload, _prediction(gold, facts=[observed]))
    assert result["summary"]["tp"] == 1


def test_no_positive_outputs_has_undefined_precision():
    gold = _gold(facts=[_fact()])
    result = evaluate(_payload(gold), _prediction(gold, state="incomplete", facts=[]))
    assert result["summary"]["determinate_output_precision"] is None
