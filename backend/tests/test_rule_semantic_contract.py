import pytest
from pydantic import ValidationError

from app.rules.semantic_contract import normalize_rule_semantics, rule_semantic_hash
from app.rules.spec import RuleSpec
from app.schemas.rules import RulePayload


def _rule(**kwargs):
    values = {
        "field_key": "pressure",
        "field_name_cn": "瓦斯压力",
        "data_type": "float",
        "unit": "MPa",
        "dimension_schema": {"workface": {}, "sample": {}},
        "strategy": {"agent_extract": {"synonyms": ["压力"]}},
    }
    values.update(kwargs)
    return RuleSpec(**values)


def test_legacy_multi_instance_maps_to_multi_identity_only():
    spec = _rule(multi_instance=True)
    contract = spec.semantic_contract
    assert spec.cardinality == "multi_identity"
    assert contract["scope"]["mode"] == "attachment"
    assert spec.rule_semantic_hash == rule_semantic_hash(spec)
    assert spec.cardinality != "all_occurrences"


def test_explicit_contract_is_normalized_and_dimensions_are_checked():
    spec = _rule(
        strategy={
            "semantic_contract": {
                "cardinality": "all_occurrences",
                "scope": {
                    "mode": "sections",
                    "section_ids": ["measurements"],
                    "reason": "业务章节",
                },
                "identity_dimensions": ["sample"],
                "applicable_dimensions": ["workface", "sample"],
                "retrieval": {
                    "terms": ["压力"],
                    "formula_patterns": [r"P\s*=\s*"],
                    "fallback_policy": "complete_scope",
                },
                "evidence_requirements": ["value", "unit", "object"],
            }
        }
    )
    assert spec.cardinality == "all_occurrences"
    assert spec.scope["section_ids"] == ["measurements"]
    assert spec.identity_dimensions == ["sample"]
    assert "object" in spec.semantic_contract["evidence_requirements"]

    with pytest.raises(ValueError, match="维度"):
        _rule(strategy={"semantic_contract": {"identity_dimensions": ["missing"]}})


def test_same_field_key_with_different_meaning_has_different_hash():
    measured = _rule(value_kind="measured")
    calculated = _rule(value_kind="calculated")
    assert measured.field_key == calculated.field_key
    assert measured.rule_semantic_hash != calculated.rule_semantic_hash


def test_payload_validates_and_persists_contract_inside_strategy():
    payload = RulePayload(
        checklist_type="gas",
        field_key="pressure",
        field_name_cn="瓦斯压力",
        dimension_schema={"sample": {}},
        strategy={
            "semantic_contract": {
                "cardinality": "multi_identity",
                "identity_dimensions": ["sample"],
                "applicable_dimensions": ["sample"],
                "unit_policy": {"mode": "source_evidence", "conversion_policy_ids": ["mpa-v1"]},
            }
        },
    )
    contract = payload.strategy["semantic_contract"]
    assert contract["contract_version"] == 2
    assert contract["scope"]["mode"] == "attachment"


def test_scope_constraints_are_rejected():
    with pytest.raises(ValidationError):
        RulePayload(
            checklist_type="gas",
            field_key="pressure",
            field_name_cn="压力",
            strategy={
                "semantic_contract": {
                    "scope": {"mode": "explicit_ranges", "ranges": [{"start": 8, "end": 2}]}
                }
            },
        )


def test_normalization_is_detached_from_source_rule():
    source = {"field_key": "x", "field_name_cn": "X", "strategy": {}}
    snapshot = normalize_rule_semantics(source)
    snapshot["retrieval"]["terms"].append("changed")
    assert "changed" not in source["strategy"]


def test_snapshot_records_legacy_adaptation_and_is_detached():
    parent = _rule(multi_instance=True)
    snapshot = parent.semantic_snapshot()
    assert snapshot["adapted_from_contract_version"] == 1
    assert snapshot["rule_version"] == parent.version
    child = _rule(strategy=parent.strategy)
    child.strategy["agent_extract"]["synonyms"].append("新词项")
    snapshot["dimension_schema"]["sample"]["description"] = "修改"
    assert parent.strategy["agent_extract"]["synonyms"] == ["压力"]
    assert parent.dimension_schema["sample"] == {}


@pytest.mark.parametrize(
    "change",
    [
        {"unit": "kPa"},
        {"field_description": "仅提取实测"},
        {"strategy": {"semantic_contract": {"cardinality": "all_occurrences"}}},
        {
            "strategy": {
                "semantic_contract": {
                    "scope": {
                        "mode": "explicit_ranges",
                        "ranges": [{"start": 1, "end": 2}],
                        "reason": "业务范围",
                    }
                }
            }
        },
        {"strategy": {"semantic_contract": {"retrieval": {"ocr_variants": ["瓦斯压カ"]}}}},
        {"strategy": {"semantic_contract": {"evidence_requirements": ["value", "value_kind"]}}},
    ],
)
def test_every_extraction_affecting_change_invalidates_identity(change):
    assert _rule().rule_semantic_hash != _rule(**change).rule_semantic_hash


def test_publishing_metadata_does_not_change_semantic_identity():
    assert (
        _rule().rule_semantic_hash
        == _rule(version=8, priority=1, source="config").rule_semantic_hash
    )


def test_config_adapter_preserves_explicit_v2_and_old_multi_instance():
    old = RuleSpec.from_config({"field_key": "gas", "field_name": "含量", "multi_instance": True})
    assert old.cardinality == "multi_identity"
    new = RuleSpec.from_config(
        {"field_key": "gas", "field_name": "含量", "cardinality": "all_occurrences"}
    )
    assert new.cardinality == "all_occurrences"


def test_payload_rejects_undeclared_dimensions_and_unverified_type_policy():
    for contract in (
        {"identity_dimensions": ["missing"]},
        {"value_kind_policy": {"unverified": "calculated"}},
        {"value_kind_policy": {"allowed_bases": []}},
        {"unit_policy": {"mode": "not_applicable"}},
    ):
        with pytest.raises(ValidationError):
            RulePayload(
                checklist_type="gas",
                field_key="gas",
                field_name_cn="含量",
                unit="MPa",
                strategy={"semantic_contract": contract},
            )


def test_legacy_retrieval_hints_are_validated_and_synonyms_preserved():
    payload = RulePayload(
        checklist_type="gas",
        field_key="gas",
        field_name_cn="含量",
        strategy={"agent_extract": {"synonyms": ["瓦斯含量"], "retrieval": {"labels": ["W"]}}},
    )
    semantics = normalize_rule_semantics(payload.model_dump())
    assert semantics["retrieval"]["labels"] == ["W"]
    assert semantics["retrieval"]["synonyms"] == ["瓦斯含量"]
