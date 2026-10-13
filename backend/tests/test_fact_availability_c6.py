"""方案 C6（2026-10-10）：清单代理事实的下游可用性。

partial_candidates、not_found 和 incomplete 不会进入下游；部分完成状态按字段判断，
因此同次运行其他字段的失败不会隐藏已完成字段。
"""

from types import SimpleNamespace
from uuid import uuid4

from app.services.fact_service import build_fact_view


def run():
    return SimpleNamespace(
        parse_run_id=uuid4(),
        document_id=uuid4(),
        extraction_profile={"rules": [{
            "field_key": "res", "data_type": "float", "unit": "m³/t",
            "value_kind": None,
            "dimension_schema": {"workface": {"required": True}},
            "strategy": {"agent_extract": {"identity_dimensions": ["workface"]}},
        }]},
    )


def registry():
    return {"ev-1": {"evidence_id": "ev-1", "page_number": 10, "bbox": [50, 100, 300, 110],
                     "location_status": "text_block", "scope": "text"}}


def instance(**kwargs):
    base = {
        "value": "2.23", "raw_value": "2.23", "unit": "m³/t", "value_kind": "measured",
        "source_role": "report_body", "dimensions": {"workface": "102回采工作面"},
        "page_num": 10, "context": "残余瓦斯含量 2.23 m³/t", "evidence_ids": ["ev-1"],
        "evidence_roles": {"ev-1": ["value", "unit"]}, "issues": [], "quality_issues": [],
        "source": "agent_extract", "confidence": None, "quality_status": "REVIEW_REQUIRED",
        "occurrence_id": "occ-1-ev-1",
    }
    base.update(kwargs)
    return base


def field(status, *, coverage=None, detail_failures=None):
    detail = {"evidence_registry": registry(), "coverage": coverage or {"status": "BOUNDED_SEARCH"}}
    if detail_failures is not None:
        detail["failures"] = detail_failures
    return SimpleNamespace(
        field_id=uuid4(), field_key="res", field_name="残余瓦斯含量",
        field_value="2.23", instances=[instance()], extraction_detail=detail,
        extraction_method="agent_extract", confidence=0.0, unit="m³/t",
        validation_status=status,
    )


def fact_for(status, **kwargs):
    facts = build_fact_view(run(), [field(status, **kwargs)]).facts
    assert len(facts) == 1
    return facts[0]


def test_review_required_with_complete_search_is_available():
    assert fact_for("review_required").available_for_downstream is True


def test_candidates_awaiting_confirmation_are_not_available():
    fact = fact_for("partial_candidates", coverage={"status": "UNRESOLVED", "failures": ["token_budget_exhausted"]})
    assert fact.available_for_downstream is False
    assert "VALIDATION_STATUS" in {r["code"] for r in fact.risk}


def test_searched_but_absent_and_incomplete_statuses_are_never_available():
    for status in ("not_found", "incomplete"):
        fact = fact_for(status)
        assert fact.available_for_downstream is False, status


def test_a_failure_recorded_for_another_field_does_not_make_this_field_partial():
    fact = fact_for("review_required",
                    coverage={"status": "BOUNDED_SEARCH", "failures": []},
                    detail_failures=["token_budget_exhausted"])
    assert fact.available_for_downstream is True


def test_this_fields_own_failure_makes_it_partial():
    fact = fact_for("review_required",
                    coverage={"status": "BOUNDED_SEARCH", "failures": ["candidates_truncated"]})
    assert fact.available_for_downstream is False
