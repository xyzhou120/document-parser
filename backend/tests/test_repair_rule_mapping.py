from types import SimpleNamespace

import pytest

from app.rules.agent_knowledge import agent_seed_rules
from app.rules.spec import RuleSpec
from scripts.repair_reliable_incident import compatible_rule


@pytest.mark.parametrize("field_key,value_type,expected_type", [
    ("gas_content_point", "point_value", "float"),
    ("predrain_period", "period", "string"),
    ("design_borehole_params", "borehole_params", "string"),
    ("design_hole_count", "number", "integer"),
])
def test_reference_fields_use_platform_literal_values_and_multi_unit_scope(field_key, value_type, expected_type):
    template = agent_seed_rules()[0]
    template.update(field_key=field_key, validation={"source_definition": "pdf_extract.fields.yaml", "value_type": value_type})
    rule = SimpleNamespace(**template, rule_id="test", version=1, created_at=None, updated_at=None)
    payload = compatible_rule(rule)
    assert payload["data_type"] == expected_type
    assert payload["multi_instance"] is True
    contract = payload["strategy"]["semantic_contract"]
    assert contract["cardinality"] != "single"
    assert "measurement_point" in contract["identity_dimensions"] if value_type == "point_value" else "workface" in contract["identity_dimensions"]
    RuleSpec(**{key: value for key, value in payload.items() if key in RuleSpec.__dataclass_fields__})
