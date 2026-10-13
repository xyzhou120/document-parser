import pytest

from app.rules.spec import RuleSpec
from app.services.evidence_verification import validate_candidate_decision


def verify(value="2025年3月16日", text="开工日期：2025年3月16日", *, expected="reported",
           observed="reported", quote=None, receipt_text=None, source="native"):
    rule = RuleSpec(field_key="date", field_name_cn="施工日期", data_type="string", value_kind=expected,
                    strategy={"semantic_contract": {"cardinality": "single", "applicable_dimensions": [],
                                "unit_policy": {"mode": "not_applicable"}, "value_kind_policy": {}}})
    candidate = {"candidate_id": "c", "text": text, "source": source, "table": False,
                 "content_revision": "r", "segment_ids": []}
    decision = {"candidate_id": "c", "judgement": "accepted", "value": value, "value_kind": observed,
                "source_role": "report_body", "evidence_ids": ["e"], "quote": quote or text}
    receipt = {"candidate_id": "c", "content_revision": "r", "source": "native_text",
               "text": text if receipt_text is None else receipt_text,
               "attributes": {"source_role": {"status": "supported", "value": "report_body", "basis": "document_structure"}}}
    return validate_candidate_decision(decision, candidate, rule, {"e": receipt})


@pytest.mark.parametrize("value,text", [("2025年3月16日", "开工日期：2025年3月16日"),
                                         ("3500", "日产量3500吨")])
def test_generic_reported_literal_does_not_require_reported_wording(value, text):
    result = verify(value, text)
    assert result["accepted"]
    assert result["assertions"]["value_kind"]["basis"] == ["source.reported_literal"]


@pytest.mark.parametrize("kind", ["measured", "calculated", "design", "actual", "threshold", "unknown"])
def test_reported_literal_never_infers_specific_or_unknown_kind(kind):
    result = verify(expected=kind, observed=kind)
    assert not result["accepted"]
    assert result["assertions"]["value_kind"]["status"] == "unknown"


@pytest.mark.parametrize("kwargs", [
    {"expected": "measured"}, {"observed": "unknown"},
    {"quote": "开工日期：2025年3月17日"},
    {"receipt_text": "开工日期：2025年3月17日"},
    {"text": "开工日期：2025年3月17日"},
    {"source": "ocr"},
])
def test_reported_requires_matching_expected_observed_and_bound_literal_sources(kwargs):
    result = verify(**kwargs)
    assert not result["accepted"]
    assert result["assertions"]["value_kind"]["status"] == "unknown"
