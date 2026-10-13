"""P10 语义和证据闭合测试。"""

from app.rules.spec import RuleSpec
from app.services.evidence_verification import validate_candidate_decision
from app.services.field_completion import complete_field
from app.services.model_gateway import CandidateInput


def rule(**kwargs):
    strategy = kwargs.pop("strategy", {"semantic_contract": {
        "cardinality": "multi_identity", "identity_dimensions": ["unit"],
        "applicable_dimensions": ["unit"], "value_kind_policy": {},
        "unit_policy": {"mode": "source_evidence"}, "evidence_requirements": ["value"]
    }})
    return RuleSpec(field_key="gas", field_name_cn="可解吸瓦斯含量", data_type="float",
        unit="m³/t", dimensions=["unit"], dimension_schema={"unit": {"required": True}},
        strategy=strategy, **kwargs)


def candidate(cid="c1", text="评价单元A 可解吸瓦斯含量 0.18 m³/t 实测", source="native", table=False):
    return {"candidate_id": cid, "text": text, "context": "评价单元A", "source": source,
            "table": table, "state": "accepted", "page_number": 1, "content_revision": "rev-1",
            "segment_ids": []}


def receipt(source="native_text", revision="rev-1", **kwargs):
    return {"source": source, "content_revision": revision, "page": 1, "bbox": [0, 0, 1, 1],
        "candidate_id": "c1", "text": candidate()["text"],
        "attributes": {"source_role": {"value": "report_body", "status": "supported", "basis": "document_structure"}},
        **kwargs}


def decision(cid="c1", **kwargs):
    return {"candidate_id": cid, "judgement": "accepted", "value": "0.18", "unit": "m³/t",
            "value_kind": "measured", "source_role": "report_body", "dimensions": {"unit": "评价单元A"},
            "evidence_ids": ["ev-text"], **kwargs}


def closed_range():
    return {"scan_done": True, "state": "scanned", "quality_blockers": [], "revision_id": "rev-1",
            "strategy_version": "complete_scope-v2", "range_key": "p1-p1"}


def fact(value, identity, cid):
    assertions = {key: {"status": "supported", "basis": "literal", "evidence_ids": ["ev-text"]}
                  for key in ("value", "unit", "value_kind", "source_role", "dimensions.unit")}
    assertions["table_relation"] = {"status": "not_applicable", "basis": "source.non_table", "evidence_ids": []}
    return {"candidate_id": cid, "value": value, "unit": "m³/t", "value_kind": "measured",
        "source_role": "report_body", "content_revision": "rev-1", "source_validity": "valid",
        "assertions": assertions, "dimensions": {"unit": identity}}


def test_acceptance_requires_platform_receipt_and_explicit_attributes():
    result = validate_candidate_decision(decision(), candidate(), rule(),
        {"ev-text": receipt()})
    assert result["accepted"] is True
    assert result["fact"]["source_role"] == "report_body"


def test_fake_image_without_revision_or_source_does_not_prove_ocr_semantics():
    c = candidate(source="ocr")
    result = validate_candidate_decision(decision(evidence_ids=["fake"]), c, rule(),
        {"fake": {"image_checked": True, "url": "data:image/png;base64,fake"}})
    codes = {item["code"] for item in result["issues"]}
    assert result["accepted"] is False
    assert {"EVIDENCE_SOURCE_UNDECLARED", "EVIDENCE_REVISION_UNDECLARED", "OCR_IMAGE_REQUIRED"} <= codes


def test_dgc_measured_text_cannot_be_declared_calculated():
    d = decision(value_kind="calculated")
    result = validate_candidate_decision(d, candidate(), rule(), {"ev-text": receipt()})
    assert "VALUE_KIND_UNVERIFIED" in {item["code"] for item in result["issues"]}


def test_not_found_requires_closed_ranges_and_terminal_candidates():
    result = complete_field(rule(), [closed_range()],
        [{"candidate_id": "c1", "state": "rejected"}], [])
    assert result["semantic_state"] == "not_found"
    assert result["absence_search_proof"]["closed_ranges"] == 1
    open_result = complete_field(rule(), [{"scan_done": False, "state": "scanning", "quality_blockers": []}],
        [{"candidate_id": "c1", "state": "pending"}], [])
    assert open_result["semantic_state"] == "incomplete"
    assert open_result["absence_search_proof"] is None


def test_same_identity_conflict_is_preserved_and_does_not_complete():
    facts = [
        fact("0.18", "评价单元A", "a"),
        fact("0.20", "评价单元A", "b"),
    ]
    result = complete_field(rule(), [closed_range()],
        [{"candidate_id": "a", "state": "accepted"}, {"candidate_id": "b", "state": "accepted"}], facts)
    assert result["semantic_state"] == "review_required"
    assert any(item["code"] == "SAME_IDENTITY_CONFLICT" for item in result["risks"])


def test_different_identity_values_can_be_completed():
    facts = [
        fact("0.18", "评价单元A", "a"),
        fact("0.20", "评价单元B", "b"),
    ]
    result = complete_field(rule(), [closed_range()],
        [{"candidate_id": "a", "state": "accepted"}, {"candidate_id": "b", "state": "accepted"}], facts)
    assert result["semantic_state"] == "completed"


def test_all_occurrences_requires_scope_close():
    all_rule = rule(strategy={"semantic_contract": {"cardinality": "all_occurrences",
        "identity_dimensions": ["unit"], "applicable_dimensions": ["unit"],
        "value_kind_policy": {}, "unit_policy": {"mode": "source_evidence"},
        "evidence_requirements": ["value"]}})
    result = complete_field(all_rule, [{"scan_done": True, "state": "scanned", "quality_blockers": ["unknown"]}], [], [])
    assert result["semantic_state"] == "incomplete"


def image_receipt(**kwargs):
    attrs = {key: {"status": "supported", "value": value, "basis": basis} for key, value, basis in (
        ("value", "0.18", "original_image_transcription"), ("unit", "m³/t", "header"),
        ("value_kind", "measured", "form_semantics"), ("dimensions.unit", "评价单元A", "header"),
        ("source_role", "report_body", "document_structure"))}
    return receipt("original_image", image_ref="image-1", region_id="region-1",
        verification={"status": "verified", "transcription": candidate()["text"], "attributes": attrs}, **kwargs)


def test_original_image_attribute_receipt_is_required_and_candidate_input_supported():
    c = CandidateInput("c1", candidate(source="ocr"), image_refs=("image-1",), source="ocr")
    result = validate_candidate_decision(decision(evidence_ids=["image"]), c, rule(), {"image": image_receipt()})
    assert result["accepted"]
    assert result["assertions"]["value_kind"]["basis"] == ["form_semantics"]
    image = image_receipt()
    image["verification"].pop("attributes")
    invalid = validate_candidate_decision(decision(evidence_ids=["image"]), c, rule(), {"image": image})
    assert not invalid["accepted"]


def test_ocr_correction_waits_for_new_revision_publication():
    c = candidate(text="评价单元A 可解吸瓦斯含量 0.1B m³/t 实测", source="ocr")
    image = image_receipt(requires_publication=True)
    result = validate_candidate_decision(decision(evidence_ids=["image"]), c, rule(), {"image": image})
    assert not result["accepted"]
    assert "OCR_CORRECTION_PENDING_PUBLICATION" in {issue["code"] for issue in result["issues"]}


def test_wrong_revision_or_wrong_candidate_receipt_cannot_support_fact():
    for image in (image_receipt(content_revision="rev-old"), image_receipt(candidate_id="other")):
        result = validate_candidate_decision(decision(evidence_ids=["image"]), candidate(source="ocr"), rule(), {"image": image})
        assert not result["accepted"]


def test_unit_and_kind_cannot_be_borrowed_from_a_different_value_in_same_row():
    c = candidate(text="评价单元A 实测 0.18 MPa；计算 2.3 m³/t")
    result = validate_candidate_decision(decision(value_kind="calculated"), c, rule(), {"ev-text": receipt(text=c["text"])})
    assert result["assertions"]["value_kind"]["status"] == "unknown"
    assert result["assertions"]["unit"]["status"] == "unknown"


def test_rule_expected_kind_and_unit_are_never_used_to_fill_missing_attributes():
    result = validate_candidate_decision(decision(value_kind=None, unit=None), candidate(), rule(value_kind="calculated"), {"ev-text": receipt()})
    assert result["fact"]["value_kind"] is None
    assert result["fact"]["unit"] is None
    assert not result["accepted"]


def test_unresolved_or_accepted_without_fact_cannot_prove_absence():
    for state in ("unresolved", "accepted", "pending"):
        result = complete_field(rule(), [closed_range()], [{"candidate_id": "a", "state": state}], [])
        assert result["semantic_state"] == "incomplete"
        assert result["absence_search_proof"] is None


def test_missing_frozen_range_and_unknown_source_coverage_block_not_found():
    envelope = {"items": [closed_range()], "expected_keys": ["complete_scope-v2:p1-p1", "complete_scope-v2:p2-p2"], "revision_id": "rev-1"}
    assert complete_field(rule(), envelope, [], [])["semantic_state"] == "incomplete"
    for state in ("unknown", "failed", "unreadable"):
        assert complete_field(rule(), [{**closed_range(), "coverage_status": state}], [], [])["semantic_state"] == "incomplete"


def test_unsupported_attributes_and_unknown_identity_cannot_complete():
    unverified = fact("0.18", "unknown", "a")
    unverified["assertions"].pop("value_kind")
    result = complete_field(rule(), [closed_range()], [{"candidate_id": "a", "state": "accepted"}], [unverified])
    assert result["semantic_state"] == "review_required"
    assert {"FACT_ATTRIBUTES_UNSUPPORTED", "IDENTITY_UNVERIFIED"} <= {risk["code"] for risk in result["risks"]}


def test_duplicate_occurrences_merge_evidence_without_false_conflict():
    first = fact("0.18", "评价单元A", "a")
    second = fact("0.180", "评价单元A", "b")
    first["evidence_ids"], second["evidence_ids"] = ["one"], ["two"]
    result = complete_field(rule(), [closed_range()], [{"candidate_id": "a", "state": "accepted"}, {"candidate_id": "b", "state": "accepted"}], [first, second])
    assert result["semantic_state"] == "completed"
    assert len(result["facts"]) == 1
    assert result["facts"][0]["evidence_ids"] == ["one", "two"]


def test_not_applicable_requires_explicit_rule_applicability_and_bound_proof():
    result = complete_field(rule(), [closed_range()], [], [], applicability={"status": "not_applicable", "proof": "not present"})
    assert result["semantic_state"] == "incomplete"
    assert "NOT_APPLICABLE_UNPROVEN" in {risk["code"] for risk in result["risks"]}


def test_material_source_role_must_belong_to_frozen_rule_vocabulary():
    configured = rule()
    configured.strategy["agent_extract"] = {"source_roles": ["report_body", "report_sheet"]}
    valid = validate_candidate_decision(decision(), candidate(), configured, {"ev-text": receipt()})
    assert valid["accepted"]
    for observed_role in ("measurement_result", "calculation_result"):
        source = receipt(attributes={"source_role": {
            "status": "supported", "value": observed_role, "basis": "source.wording:实测",
        }})
        result = validate_candidate_decision(decision(source_role=observed_role), candidate(), configured,
                                             {"ev-text": source})
        assert not result["accepted"]
        assert result["assertions"]["source_role"]["status"] == "mismatch"
        assert result["assertions"]["source_role"]["observed"] == observed_role
        assert result["assertions"]["source_role"]["expected"] == ["report_body", "report_sheet"]
        assert "SOURCE_ROLE_UNCONFIGURED" in {issue["code"] for issue in result["issues"]}


def test_material_source_role_requires_receipt_even_when_configured():
    configured = rule()
    configured.strategy["agent_extract"] = {"source_roles": ["report_body"]}
    bare_receipt = receipt(attributes={})
    result = validate_candidate_decision(decision(source_role="report_body"), candidate(), configured,
                                         {"ev-text": bare_receipt})
    assert not result["accepted"]
    assert result["assertions"]["source_role"]["status"] == "unknown"
