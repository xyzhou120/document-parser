import pytest

from app.rules.spec import RuleSpec
from app.services.evidence_verification import validate_candidate_decision, value_in_text


@pytest.mark.parametrize("text,value", [
    ("运顺1#-1 3.42 0.23", "3.42"),
    ("运顺3#-2 3.99 0.28 运顺4#-1 4.91 0.36", "4.91"),
    ("测点1\t0.18\t2.5", "0.18"),
    ("瓦斯量 0.180 m³/t", "0.18"),
])
def test_literal_numbers_preserve_adjacent_column_boundaries(text, value):
    assert value_in_text(value, text, True)


@pytest.mark.parametrize("value", ["3.4", "13.42", "3.42023", "NaN"])
def test_partial_or_merged_numbers_never_match(value):
    assert not value_in_text(value, "运顺1#-1 3.42 0.23", True)


def test_real_table_style_candidate_retains_value_receipt_binding():
    rule = RuleSpec(field_key="gas", field_name_cn="含量", data_type="float", unit="m3/t",
                    value_kind="measured", strategy={"semantic_contract": {
                        "cardinality": "single", "applicable_dimensions": [],
                        "unit_policy": {"mode": "source_evidence"}, "value_kind_policy": {}}})
    text = "（m3/t） 测定值 运顺1#-1 3.42 0.23"
    candidate = {"candidate_id": "c", "text": text, "source": "native",
                 "content_revision": "rev", "segment_ids": [], "table": True}
    decision = {"candidate_id": "c", "judgement": "accepted", "value": "3.42",
                "unit": "m3/t", "value_kind": "measured", "source_role": "report_body",
                "evidence_ids": ["e"]}
    receipt = {"source": "native_text", "content_revision": "rev", "candidate_id": "c",
               "segment_ids": [], "text": text}
    result = validate_candidate_decision(decision, candidate, rule, {"e": receipt})
    assert result["assertions"]["value"]["status"] == "supported"
    assert result["assertions"]["value"]["evidence_ids"] == ["e"]
    assert result["assertions"]["unit"]["status"] == "unknown"  # Header binding remains required.


def test_ocr_literal_numbers_still_require_verified_original_image():
    rule = RuleSpec(field_key="gas", field_name_cn="含量", data_type="float", value_kind="reported")
    candidate = {"candidate_id": "c", "text": "瓦斯量 0.18 2.5", "source": "ocr",
                 "content_revision": "rev", "segment_ids": [], "table": False}
    decision = {"candidate_id": "c", "judgement": "accepted", "value": "0.18",
                "value_kind": "reported", "evidence_ids": ["e"]}
    receipt = {"source": "ocr_text", "content_revision": "rev", "candidate_id": "c", "text": candidate["text"]}
    result = validate_candidate_decision(decision, candidate, rule, {"e": receipt})
    assert result["assertions"]["value"]["status"] == "unknown"
    assert not result["usable"]
