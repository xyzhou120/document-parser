"""P12 reliable views preserve v1 and require P10 complete attribute support."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from app.api import stats
from app.api.stats import _field_inventory
from app.services.fact_policy_v2 import VERSION, matching_invalidations
from app.services.fact_service import build_fact_view


def run():
    return SimpleNamespace(parse_run_id=uuid4(), document_id=uuid4(), contract_version=VERSION,
        status="COMPLETED", content_revision_id="rev-1", extraction_profile={"rules": [{
        "field_key": "gas", "data_type": "float", "unit": "m³/t",
        "dimension_schema": {"face": {"required": True}},
        "semantic_contract": {"cardinality": "multi_identity", "identity_dimensions": ["face"],
            "applicable_dimensions": ["face"], "unit_policy": {"mode": "source_evidence"}},
    }]})


def item(value="2.23", face="A", **kwargs):
    assertions = {key: {"status": "supported", "basis": "literal", "evidence_ids": ["ev"]}
        for key in ("value", "unit", "value_kind", "source_role", "dimensions.face")}
    assertions["table_relation"] = {"status": "not_applicable", "basis": "source.non_table", "evidence_ids": []}
    return {"value": value, "unit": "m³/t", "value_kind": "measured", "source_role": "report",
        "dimensions": {"face": face}, "candidate_id": face + value, "occurrence_id": face + value,
        "content_revision": "rev-1", "assertions": assertions, "source_validity": "valid",
        "evidence_ids": ["ev"], "page_num": 1, "quote": face + value, **kwargs}


def field(items=None, **kwargs):
    detail = {"contract_version": VERSION, "semantic_state": "completed", "coverage_complete": True,
        "instances": [item()] if items is None else items, "evidence_registry": {"ev": {"page_number": 1, "block_id": "b1"}},
        "coverage": {"status": "COMPLETE"}}
    detail.update(kwargs.pop("detail", {}))
    base = {"field_key": "gas", "field_name": "瓦斯", "field_value": None, "confidence": None,
        "extraction_method": "agent_extract", "validation_status": "completed", "extraction_detail": detail,
        "instances": detail["instances"]}
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_complete_supported_fact_is_available_with_v2_schema():
    view = build_fact_view(run(), [field()])
    assert view.contract_version == VERSION
    assert view.schema_version == "v2"
    fact = view.facts[0]
    assert fact.available_for_downstream is True
    assert fact.automatic_quality_status == "PASSED"
    assert fact.assertions["value_kind"]["status"] == "supported"
    assert fact.coverage_complete is True


@pytest.mark.parametrize("changes", [
    {"value_kind": "unknown"}, {"dimensions": {"face": "unknown"}},
    {"source_validity": "unknown"}, {"content_revision": "old"},
])
def test_unknown_or_wrong_revision_attributes_are_not_released(changes):
    fact = build_fact_view(run(), [field([item(**changes)])]).facts[0]
    assert fact.available_for_downstream is False
    assert fact.availability_reasons


def test_missing_assertions_and_partial_or_review_semantics_block_availability():
    for detail in ({"coverage_complete": False}, {"semantic_state": "review_required"}, {"semantic_state": "partial_candidates"}):
        assert not build_fact_view(run(), [field(detail=detail)]).facts[0].available_for_downstream
    unverified = item()
    unverified["assertions"].pop("value_kind")
    assert not build_fact_view(run(), [field([unverified])]).facts[0].available_for_downstream


def test_different_identity_values_are_available_same_identity_conflicts_are_blocked():
    current = run()
    different = build_fact_view(current, [field([item("2.23", "A"), item("3.38", "B")])]).facts
    assert len(different) == 2 and all(f.available_for_downstream for f in different)
    conflicts = build_fact_view(current, [field([item("2.23", "A"), item("3.38", "A")])]).facts
    assert len(conflicts) == 2 and all(not f.available_for_downstream for f in conflicts)


def test_invalidation_by_revision_field_fact_and_evidence_is_applied_without_deleting_history():
    current, row = run(), field()
    original = build_fact_view(current, [row]).facts[0]
    selectors = ({"scope": "revision", "revision_id": "rev-1", "request_id": "x"},
        {"scope": "field", "field_keys": ["gas"]}, {"scope": "fact", "fact_id": original.fact_id},
        {"scope": "evidence", "evidence_ids": ["ev"]})
    for selector in selectors:
        invalidation = {"document_id": current.document_id, "invalidation_id": "i1", "scope_selector": selector,
                        "reason": "corrected source", "effective_at": datetime.now(UTC).replace(tzinfo=None)}
        view = build_fact_view(current, [row], invalidations=[invalidation])
        fact = view.facts[0]
        assert fact.source_validity == "invalid" and not fact.available_for_downstream
        assert fact.invalidation_reason == "corrected source"
    assert build_fact_view(current, [row]).facts[0].available_for_downstream


def test_future_and_other_document_invalidation_do_not_match():
    current = run()
    fact = build_fact_view(current, [field()]).facts[0]
    assert not matching_invalidations(current, fact, [{"document_id": uuid4(), "scope_selector": {}}])
    future = datetime.now(UTC).replace(tzinfo=None) + timedelta(days=1)
    assert not matching_invalidations(current, fact, [{"document_id": current.document_id, "scope_selector": {}, "effective_at": future}])


def test_v2_inventory_distinguishes_found_usable_review_and_unfinished():
    current = run()
    rule = current.extraction_profile["rules"][0]
    good = _field_inventory(field(), rule, run=current)
    assert good["candidate_found"] == good["available_for_downstream"] == good["coverage_complete"] == 1
    assert good["review_needed"] == good["unfinished"] == 0
    pending = _field_inventory(field(detail={"semantic_state": "partial_candidates", "coverage_complete": False}), rule, run=current)
    assert pending["candidate_found"] == pending["unfinished"] == 1
    assert pending["available_for_downstream"] == 0


def test_fact_projection_does_not_mutate_automatic_source_rows():
    row = field()
    before = deepcopy(row.extraction_detail)
    build_fact_view(run(), [row])
    assert row.extraction_detail == before


def test_available_view_filters_review_candidates_and_invalidated_sources():
    current = run()
    assert len(build_fact_view(current, [field()], view="available").facts) == 1
    assert build_fact_view(current, [field(detail={"semantic_state": "review_required"})], view="available").facts == []
    assert build_fact_view(current, [field()], view="available", invalidations=[{
        "document_id": current.document_id, "scope_selector": {"revision_id": "rev-1"}, "reason": "bad text"
    }]).facts == []


def test_all_occurrences_keeps_distinct_locations_even_when_value_is_identical():
    current = run()
    current.extraction_profile["rules"][0]["semantic_contract"]["cardinality"] = "all_occurrences"
    left, right = item(), item(occurrence_id="second-location", candidate_id="second")
    facts = build_fact_view(current, [field([left, right])]).facts
    assert len(facts) == 2


@pytest.mark.asyncio
async def test_statistics_include_v2_partial_runs_without_counting_them_completed():
    current = run()
    current.checklist_type = "test"
    current.status = "PARTIAL"
    row = field(detail={"coverage_complete": False, "semantic_state": "partial_candidates"})
    row.parse_run_id = current.parse_run_id
    row.required = True
    def result(rows):
        return Mock(scalars=Mock(return_value=Mock(all=Mock(return_value=rows))))
    db = Mock(execute=AsyncMock(side_effect=[result([current]), result([row]), result([])]))
    response = await stats.field_quality(checklist_type=None, limit=30, db=db)
    assert response["completed_runs"] == 0
    assert response["observed_runs"] == 1
    assert response["items"][0]["unfinished"] == 1
    assert response["items"][0]["available_for_downstream"] == 0
