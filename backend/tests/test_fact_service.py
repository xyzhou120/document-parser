from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

from app.services.fact_service import build_fact_view, compare_fact_views


def run(**kwargs):
    return SimpleNamespace(
        parse_run_id=uuid4(),
        document_id=uuid4(),
        extraction_profile={
            "rules": [
                {
                    "field_key": "count",
                    "data_type": "integer",
                    "unit": "孔",
                    "dimension_schema": {"face": {"required": True}},
                }
            ]
        },
        **kwargs,
    )


def field(items, **kwargs):
    props = {
        "field_id": uuid4(),
        "field_key": "count",
        "field_name": "孔数",
        "field_value": None,
        "instances": items,
        "extraction_detail": {},
        "extraction_method": "keyword",
        "confidence": 0.95,
        "unit": "孔",
        "validation_status": "passed",
    }
    props.update(kwargs)
    return SimpleNamespace(**props)


def item(value="120", face="A", **kwargs):
    return {
        "value": value,
        "raw_value": value,
        "unit": "孔",
        "value_kind": "actual",
        "source_role": "report",
        "dimensions": {"face": face},
        "page_num": 1,
        "context": f"{face}工作面 {value}孔",
        **kwargs,
    }


def codes(fact):
    return {r["code"] for r in fact.risk}


def test_same_identity_merges_evidence_other_identities_and_unknowns_do_not():
    current = run()
    rows = [item(), item("120.0", page_num=2), item(face="B"), item(face=None), item(face=None)]
    source = deepcopy(rows)
    facts = build_fact_view(current, [field(rows)]).facts
    assert len(facts) == 4
    known = next(f for f in facts if f.dimensions["face"] == "A")
    assert known.normalized == "120"
    assert {e["page_number"] for e in known.evidence} == {1, 2}
    assert known.confidence.kind == "heuristic_uncalibrated"
    assert known.confidence.calibration_scope is None
    assert [f.fact_id for f in facts] == [
        f.fact_id for f in build_fact_view(current, [field(rows)]).facts
    ]
    assert rows == source


def test_same_identity_conflicts_preserved_but_other_objects_are_not_conflicts():
    facts = build_fact_view(run(), [field([item("120"), item("173"), item("999", face="B")])]).facts
    assert len(facts) == 3
    for fact in facts:
        assert ("SAME_IDENTITY_VALUE_CONFLICT" in codes(fact)) == (fact.dimensions["face"] == "A")
        assert fact.available_for_downstream is True  # Downstream may assess conflicts itself.


def test_agent_legacy_zero_is_unknown_and_high_scores_cannot_hide_risks():
    unknown = build_fact_view(
        run(), [field([item()], extraction_method="agent_extract", confidence=0.0)]
    ).facts[0]
    assert unknown.confidence.score is None
    assert unknown.confidence.kind == "uncalibrated_model"
    risky = build_fact_view(
        run(), [field([item(confidence=1.0, issues=[{"code": "OCR_LOW_QUALITY"}])])]
    ).facts[0]
    assert risky.confidence.score == 1.0
    assert "OCR_LOW_QUALITY" in codes(risky)
    legacy = build_fact_view(run(), [field([], field_value="120")]).facts[0]
    assert legacy.available_for_downstream is None
    assert "LEGACY_UNASSESSED" in codes(legacy)


def test_agent_groups_retain_occurrence_issues_and_dimension_evidence():
    candidate = item(
        occurrence_id="a",
        evidence_ids=["value"],
        dimensions={"face": {"value": "A", "evidence_ids": ["scope"]}},
        issues=[{"code": "NEEDS_UNIT_CHECK"}],
    )
    row = field(
        [candidate],
        extraction_method="agent_extract",
        extraction_detail={
            "value_groups": [
                {
                    "value": "120",
                    "raw_values": ["120孔"],
                    "unit": "孔",
                    "dimensions": candidate["dimensions"],
                    "value_kind": "actual",
                    "source_role": "report",
                    "evidence_ids": ["value"],
                    "occurrence_ids": ["a"],
                }
            ],
            "evidence_registry": {
                "value": {"page_number": 1, "bbox": [1, 1, 2, 2]},
                "scope": {"page_number": 2, "bbox": [1, 1, 2, 2]},
            },
            "coverage": {"status": "BOUNDED_SEARCH", "unvisited_pages": [3]},
        },
    )
    fact = build_fact_view(run(), [row]).facts[0]
    assert "NEEDS_UNIT_CHECK" in codes(fact)
    assert "PARTIAL_COVERAGE" in codes(fact)
    assert fact.available_for_downstream is False
    assert {e.get("evidence_id") for e in fact.evidence} >= {"value", "scope"}


def revision(key="count", action="CORRECT", rev=1, **kwargs):
    return SimpleNamespace(
        field_key=key,
        review_id=uuid4(),
        revision=rev,
        action=action,
        reviewer="tester",
        reason="checked",
        file_hash="a" * 64,
        created_at="2026-10-09",
        facts=kwargs.get("facts", [item("173", evidence=[{"page_number": 1}])]),
    )


def test_reviewed_view_is_explicit_latest_and_no_probability_or_automatic_fallback():
    current, source = run(), field([item()])
    automatic = build_fact_view(current, [source])
    assert build_fact_view(current, [source], view="reviewed").facts == []
    reviewed = build_fact_view(current, [source], view="reviewed", revisions=[revision()])
    assert reviewed.facts[0].value == "173"
    assert reviewed.facts[0].confidence.score is None
    assert reviewed.facts[0].review_status == "HUMAN_REVIEWED"
    assert reviewed.facts[0].provenance["automatic_confidence"] == 0.95
    rejected = build_fact_view(
        current, [source], view="reviewed", revisions=[revision(), revision(action="REJECT", rev=2)]
    )
    assert rejected.facts == []
    assert rejected.reviews[0]["review_status"] == "REJECTED"
    assert automatic.model_dump() == build_fact_view(current, [source]).model_dump()


def test_automatic_view_exposes_field_review_metadata_without_changing_source_fact():
    current, source = run(), field([item()])
    view = build_fact_view(current, [source], revisions=[revision(action="REJECT")])
    assert view.facts[0].value == "120"
    assert view.facts[0].review_status == "UNREVIEWED"
    assert view.facts[0].field_review_status == "REJECTED"
    assert view.reviews[0]["action"] == "REJECT"


def test_full_comparison_reports_values_and_quality_differences():
    before_run, after_run = run(), run()
    after_run.document_id = before_run.document_id
    before = build_fact_view(before_run, [field([item(), item(face="B")])])
    after = build_fact_view(after_run, [field([item(confidence=0.5), item("173", face="B")])])
    diff = compare_fact_views(before, after)
    assert len(diff["added"]) == len(diff["removed"]) == len(diff["changed"]) == 1
    assert diff["unchanged_count"] == 0


def test_defaults_are_not_document_facts_and_invalid_numbers_are_retained_with_risks():
    assert build_fact_view(run(), [field([item()], extraction_method="fallback")]).facts == []
    fact = build_fact_view(run(), [field([item("NaN")])]).facts[0]
    assert "INVALID_VALUE_FORMAT" in codes(fact)
    assert not fact.available_for_downstream


def test_instance_validation_quality_is_preserved_as_downstream_risk():
    from types import SimpleNamespace
    from uuid import uuid4
    run = SimpleNamespace(parse_run_id=uuid4(), document_id=uuid4(), extraction_profile={"rules": [
        {"field_key": "holes", "data_type": "integer", "unit": "个", "value_kind": "actual"}]})
    field = SimpleNamespace(field_key="holes", field_name="孔数", extraction_method="keyword", confidence=0.99,
        field_value=None, extraction_detail={}, validation_status="review_required", instances=[
            {"value": "999", "unit": "个", "value_kind": "actual", "source_role": "report_body",
             "page_num": 1, "context": "999个", "quality_status": "out_of_range"}])
    fact = build_fact_view(run, [field]).facts[0]
    assert any(r["code"] == "INSTANCE_QUALITY_STATUS" and r["status"] == "out_of_range" for r in fact.risk)
    assert fact.confidence.kind == "heuristic_uncalibrated"
