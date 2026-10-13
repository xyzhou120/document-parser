import pytest

from scripts.confidence_diagnostics import diagnose


def evidence(page=1):
    return [{"page_number": page, "bbox": [10, 10, 30, 20],
             "coordinate_system": "PDF_POINT", "role": "value"}]


def fact(value, score, *, workface="40203", kind="heuristic_uncalibrated", version="v1",
         semantics=None):
    return {"raw_value": value, "normalized_value": value, "unit": "个",
            "dimensions": {"workface": workface}, "value_kind": "actual",
            "source_role": "report_body", "evidence": evidence(),
            "confidence": {"score": score, "kind": kind, "version": version,
                           "semantics": semantics}}


def gold_field(facts=None, status="VERIFIED"):
    return {"field_key": "actual_hole_count", "status": status, "scope": "whole_document",
            "scope_pages": [], "complete": True, "annotator": "甲", "adjudicator": "乙",
            "verified_at": "2026-10-09", "facts": facts or [{**fact("2", None), "confidence": None}]}


def gold(facts=None, split="holdout"):
    return {"documents": [{"id": 1, "sha256": "a" * 64, "pages": 1, "split": split,
                            "layout": "native", "mine_group": "甲",
                            "fields": [gold_field(facts)]}]}


def predictions(facts, *, metadata=None):
    return {"documents": [{"id": 1, "sha256": "a" * 64,
                            "fields": [{"field_key": "actual_hole_count", "facts": facts}]}],
            "run_metadata": metadata or {"model": "test", "model_version": "m1"}}


def test_method_scores_are_diagnostic_and_unknown_is_retained():
    result = diagnose(gold(), predictions([fact("2", 0.95), fact("3", None)]), bins=5)
    assert result["status"] == "DIAGNOSTIC"
    group = result["groups"][0]
    assert group["unknown_n"] == 1
    assert group["brier"] is None
    assert any("semantics" in reason for reason in group["undefined_reasons"])
    assert result["summary"]["fp"] == 1
    assert result["summary"]["fn"] == 0


def test_high_score_wrong_fact_is_counted_wrong_not_promoted():
    result = diagnose(gold(), predictions([fact("999", 0.99)]), bins=10)
    assert result["summary"]["tp"] == 0
    assert result["summary"]["fp"] == 1
    assert result["groups"][0]["bins"][0]["index"] == 9
    assert result["groups"][0]["bins"][0]["correct"] == 0
    assert result["groups"][0]["brier"] is None


def test_no_verified_gold_produces_no_groups_and_no_probability():
    no_gold = gold([], split="holdout")
    no_gold["documents"][0]["fields"][0]["status"] = "NOT_ANNOTATED"
    no_gold["documents"][0]["fields"][0]["complete"] = False
    result = diagnose(no_gold, predictions([fact("2", 0.9)]))
    assert result["status"] == "NO_VERIFIED_GOLD"
    assert result["groups"] == []
    assert result["summary"]["unscored_fields"] == 1


def test_declared_probability_metrics_require_explicit_scope_and_minimum():
    correct = {**fact("2", 0.9, kind="declared_probability", semantics="fact_correct_probability"),
               "confidence": {"score": 0.9, "kind": "declared_probability", "version": "v1",
                              "semantics": "fact_correct_probability"}}
    wrong = {**fact("999", 0.1, kind="declared_probability", semantics="fact_correct_probability"),
             "confidence": {"score": 0.1, "kind": "declared_probability", "version": "v1",
                            "semantics": "fact_correct_probability"}}
    result = diagnose(gold(), predictions([correct, wrong]), bins=2, min_samples=2)
    group = result["groups"][0]
    assert group["status"] == "PROBABILITY_VALIDATION_ONLY"
    assert group["brier"] is not None
    assert group["ece"] is not None
    assert group["calibration_performed"] is False


def test_mixed_confidence_versions_and_contaminated_holdout_are_rejected():
    first = fact("2", 0.8, version="v1")
    second = fact("3", 0.7, version="v2")
    with pytest.raises(ValueError, match="Mixed confidence versions"):
        diagnose(gold(), predictions([first, second]))
    metadata = {"holdout_contaminated": True}
    with pytest.raises(ValueError, match="Contaminated holdout"):
        diagnose(gold(), predictions([fact("2", 0.8)], metadata=metadata))


def test_object_binding_and_evidence_are_required_for_correctness_but_scored_separately():
    wrong_identity = fact("2", 0.8, workface="40204")
    result = diagnose(gold(), predictions([wrong_identity]))
    assert result["summary"]["fp"] == 1
    assert result["summary"]["fn"] == 1
    assert result["labels"][0]["evidence_supported"] is False


def test_partial_scope_warns_without_claiming_full_recall():
    partial = gold()
    field = partial["documents"][0]["fields"][0]
    field.update(scope="sampled_pages", scope_pages=[1])
    result = diagnose(partial, predictions([fact("2", 0.8)]))
    assert any("whole-document recall" in warning for warning in result["warnings"])


def test_source_hash_and_field_scope_mismatch_are_rejected():
    payload = predictions([fact("2", 0.8)])
    payload["documents"][0]["sha256"] = "b" * 64
    with pytest.raises(ValueError, match="hash mismatch"):
        diagnose(gold(), payload)
    payload = predictions([fact("2", 0.8)])
    payload["documents"][0]["fields"][0]["field_key"] = "other"
    with pytest.raises(ValueError, match="field scope"):
        diagnose(gold(), payload)


def test_correct_fact_without_supported_geometry_is_separate_from_identity_accuracy():
    payload = predictions([fact("2", 0.8)])
    payload["documents"][0]["fields"][0]["facts"][0]["evidence"] = evidence(page=2)
    result = diagnose(gold(), payload, evidence_iou=0.5)
    assert result["summary"]["tp"] == 1
    assert result["summary"]["evidence_supported_tp"] == 0
    assert result["labels"][0]["evidence_supported"] is False


def test_probability_metrics_remain_undefined_without_predeclared_minimum_or_independent_split():
    payload = predictions([fact("2", 0.9, kind="declared_probability", semantics="fact_correct_probability"),
                           fact("999", 0.1, kind="declared_probability", semantics="fact_correct_probability")])
    result = diagnose(gold(), payload)
    assert result["groups"][0]["brier"] is None
    assert any("minimum" in reason for reason in result["groups"][0]["undefined_reasons"])
    result = diagnose(gold(split="dev"), payload, min_samples=2)
    assert result["groups"][0]["ece"] is None
    assert any("holdout" in reason for reason in result["groups"][0]["undefined_reasons"])


def test_frozen_version_contract_and_document_metadata_are_not_silently_mixed():
    expected = gold()
    expected["evaluation_contract"] = {"frozen_configuration": {"knowledge_hash": "old"}}
    with pytest.raises(ValueError, match="frozen configuration"):
        diagnose(expected, predictions([fact("2", 0.8)], metadata={"frozen_configuration": {"knowledge_hash": "new"}}))
    payload = predictions([fact("2", 0.8)])
    payload["documents"][0]["run_metadata"] = {"model_version": "m2"}
    with pytest.raises(ValueError, match="Mixed prediction version"):
        diagnose(gold(), payload)


@pytest.mark.parametrize("score", [float("nan"), float("inf"), True, -0.1, 1.1])
def test_invalid_score_cannot_become_a_bucket_or_probability(score):
    with pytest.raises(ValueError, match="finite number"):
        diagnose(gold(), predictions([fact("2", score)]))

