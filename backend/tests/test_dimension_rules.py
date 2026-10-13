"""规则表达可选业务维度，不预设工作面。"""
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.models.rules import ExtractionRule
from app.rules.repository import RuleRepository
from app.rules.spec import RuleSpec
from app.schemas.rules import RulePatch, RulePayload


def payload(**kwargs):
    return RulePayload(checklist_type="gas", field_key="pressure", field_name_cn="压力", **kwargs)


def test_dimensions_are_optional_and_support_non_workface_checks():
    assert payload().dimension_schema == {}
    rule = payload(dimension_schema={"pump_station": {"required": True}}, value_kind="measured")
    assert rule.dimension_schema["pump_station"].required is True
    assert "workface" not in rule.dimension_schema
    assert payload(strategy={"dimension_bindings": {"pump_station": {"column_keywords": ["泵站"]}}})


def test_dimension_semantics_are_preserved_for_the_agent_and_snapshot():
    definition = payload(dimension_schema={"roadway": {"description": "巷道名称，不是里程范围"}})
    assert definition.model_dump()["dimension_schema"]["roadway"]["description"] == "巷道名称，不是里程范围"


@pytest.mark.parametrize("schema", [
    {"工作面": {"required": True}},
    {"unit": {"parent_key": "missing"}},
    {"face": {"parent_key": "unit"}, "unit": {"parent_key": "face"}},
    {"face": {"unsupported": True}},
])
def test_invalid_dimension_schema_is_rejected(schema):
    with pytest.raises(ValidationError):
        payload(dimension_schema=schema)


def test_invalid_strategy_is_rejected_before_a_task_runs():
    for strategy in (
        {"methods": ["unknown_strategy"]},
        {"methods": ["regex"], "regex": {"pattern": "["}},
        {"keyword_search": {"regex": "("}},
        {"dimension_bindings": {"workface": {"regex": "["}}},
    ):
        with pytest.raises(ValidationError):
            payload(strategy=strategy)
    with pytest.raises(ValidationError):
        RulePatch(dimension_schema=None)
    with pytest.raises(ValidationError):
        RulePatch(value_kind="best_guess")


@pytest.mark.asyncio
async def test_dimension_contract_survives_repository_and_runtime_snapshot():
    data = payload(
        dimension_schema={"workface": {"required": True}, "unit": {"parent_key": "workface"}},
        value_kind="measured", field_description="只取压力实测，不取设计值",
    ).model_dump()
    db = Mock(add=Mock(), flush=AsyncMock(), commit=AsyncMock())
    repository = RuleRepository()
    repository._log_change = AsyncMock()
    created = await repository.create_rule(db, data)
    created.rule_id = uuid4()
    spec = RuleSpec.from_orm(created)
    assert spec.dimensions == ["workface", "unit"]
    assert spec.dimension_schema["workface"]["required"] is True
    assert spec.value_kind == "measured"
    assert "不取设计值" in spec.field_description
    assert repository.rule_to_dict(created)["dimension_schema"] == data["dimension_schema"]
    entity = ExtractionRule(**{**data, "rule_id": uuid4(), "version": 1})
    db.get = AsyncMock(return_value=entity)
    await repository.update_rule(db, entity.rule_id, {"dimension_schema": {}, "value_kind": "unknown"})
    assert entity.dimension_schema == {}
    assert entity.value_kind == "unknown"
    assert entity.version == 2
