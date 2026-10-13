"""统计使用运行所属清单和历史规则快照。"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest

from app.api import stats


def _result(values):
    return Mock(scalars=Mock(return_value=Mock(all=Mock(return_value=values))))


@pytest.mark.asyncio
async def test_field_quality_uses_snapshot_and_includes_missing_required_fields():
    run_a, run_b, run_c = uuid4(), uuid4(), uuid4()
    rules = [
        {"field_key": "gas", "field_name_cn": "瓦斯", "required": True},
        {"field_key": "date", "field_name_cn": "日期", "required": True},
    ]
    runs = [
        SimpleNamespace(parse_run_id=run_a, checklist_type="alpha", extraction_profile={"rules": rules}),
        SimpleNamespace(parse_run_id=run_b, checklist_type="alpha", extraction_profile={"rules": rules}),
        SimpleNamespace(parse_run_id=run_c, checklist_type="beta", extraction_profile={"rules": [rules[0]]}),
    ]
    fields = [
        SimpleNamespace(parse_run_id=run_a, field_key="gas", field_value="0.7", confidence=0.4,
                        validation_status="conflict", field_name="瓦斯", required=True),
        SimpleNamespace(parse_run_id=run_c, field_key="gas", field_value="1.2", confidence=0.95,
                        validation_status="passed", field_name="瓦斯", required=True),
    ]
    db = Mock(execute=AsyncMock(side_effect=[_result(runs), _result(fields)]))
    response = await stats.field_quality(checklist_type=None, limit=30, db=db)
    by_key = {(item["checklist_type"], item["field_key"]): item for item in response["items"]}
    assert by_key["alpha", "gas"]["coverage"] == 50
    assert by_key["alpha", "gas"]["review_needed"] == 2
    assert by_key["alpha", "date"]["coverage"] == 0
    assert by_key["alpha", "date"]["review_needed"] == 2
    assert by_key["beta", "gas"]["coverage"] == 100
    assert by_key["beta", "gas"]["review_needed"] == 0


@pytest.mark.asyncio
async def test_historical_run_without_snapshot_does_not_invent_coverage():
    run_id = uuid4()
    runs = [SimpleNamespace(parse_run_id=run_id, checklist_type="alpha", extraction_profile=None)]
    fields = [SimpleNamespace(parse_run_id=run_id, field_key="gas", field_value="0.7",
                             confidence=0.0, validation_status="passed", field_name="瓦斯", required=True)]
    db = Mock(execute=AsyncMock(side_effect=[_result(runs), _result(fields)]))
    response = await stats.field_quality(checklist_type="alpha", limit=30, db=db)
    assert response["items"][0]["coverage"] is None
    assert response["items"][0]["avg_confidence"] == 0
    for call in db.execute.await_args_list:
        assert "parse_runs.checklist_type" in str(call.args[0])


def test_today_starts_at_shanghai_midnight_in_utc_storage():
    local = datetime(2026, 10, 8, 5, 30, tzinfo=timezone(timedelta(hours=8)))
    with patch.object(stats, "datetime") as mocked:
        mocked.now.return_value = local
        assert stats._start_of_today() == datetime(2026, 10, 7, 16)


@pytest.mark.asyncio
async def test_stage_duration_ignores_legacy_log_counts():
    rows = [("ocr", None), ("ocr", {"duration_seconds": 2.0}),
            ("ocr", {"duration_seconds": 6.0}), ("extraction", {"duration_seconds": -1})]
    db = Mock(execute=AsyncMock(return_value=Mock(all=Mock(return_value=rows))),
              scalar=AsyncMock(return_value=12.0))
    response = await stats.stage_duration(db=db)
    assert response["stages"] == [{"stage": "ocr", "entries": 2, "avg_duration_seconds": 4.0}]


@pytest.mark.asyncio
async def test_multi_object_facts_are_found_and_available_without_a_main_value():
    run_id = uuid4()
    rule = {"field_key": "holes", "field_name_cn": "孔数", "required": True,
            "unit": "个", "value_kind": "actual",
            "dimension_schema": {"workface": {"required": True}}}
    groups = [
        {"value": "120", "unit": "个", "value_kind": "actual", "source_role": "report_body",
         "dimensions": {"workface": {"value": "A"}}, "evidence_ids": ["a"]},
        {"value": "173", "unit": "个", "value_kind": "actual", "source_role": "report_body",
         "dimensions": {"workface": {"value": "B"}}, "evidence_ids": ["b"]},
    ]
    field = SimpleNamespace(parse_run_id=run_id, field_key="holes", field_name="孔数",
                            field_value=None, confidence=None, required=True,
                            validation_status="review_required", instances=[{"value": "120"}, {"value": "173"}],
                            extraction_detail={"value_groups": groups, "evidence_registry": {
                                "a": {"page_number": 1}, "b": {"page_number": 2}}})
    runs = [SimpleNamespace(parse_run_id=run_id, checklist_type="gas",
                            extraction_profile={"rules": [rule]})]
    db = Mock(execute=AsyncMock(side_effect=[_result(runs), _result([field])]))
    item = (await stats.field_quality(checklist_type=None, limit=30, db=db))["items"][0]
    assert item["extracted"] == item["candidate_found"] == 1
    assert item["coverage"] == 100
    assert item["fact_count"] == item["value_group_count"] == 2
    assert item["available_for_downstream"] == 1
    assert item["availability_unknown"] == 0
    assert item["risk_count"] == 0


@pytest.mark.asyncio
async def test_group_only_result_is_found_and_unknown_legacy_availability_is_not_zero():
    run_id = uuid4()
    rules = [{"field_key": "group"}, {"field_key": "legacy"}, {"field_key": "default"}]
    fields = [
        SimpleNamespace(parse_run_id=run_id, field_key="group", field_name="Group", field_value=None,
                        validation_status="review_required", confidence=None,
                        extraction_detail={"value_groups": [{"value": "0", "evidence_ids": ["e"]}],
                                           "evidence_registry": {"e": {"page_number": 1}}}),
        SimpleNamespace(parse_run_id=run_id, field_key="legacy", field_name="Legacy", field_value="0",
                        validation_status="passed", confidence=0.0),
        SimpleNamespace(parse_run_id=run_id, field_key="default", field_name="Default", field_value="8",
                        validation_status="fallback_used", confidence=0.5),
    ]
    runs = [SimpleNamespace(parse_run_id=run_id, checklist_type="gas", extraction_profile={"rules": rules})]
    db = Mock(execute=AsyncMock(side_effect=[_result(runs), _result(fields)]))
    items = {x["field_key"]: x for x in (await stats.field_quality(checklist_type=None, limit=30, db=db))["items"]}
    assert items["group"]["candidate_found"] == 1
    assert items["group"]["fact_count"] == 1
    assert items["legacy"]["candidate_found"] == 1
    assert items["legacy"]["availability_unknown"] == 1
    assert items["legacy"]["available_for_downstream"] is None
    assert items["default"]["candidate_found"] == 0


@pytest.mark.asyncio
async def test_same_identity_different_values_and_unbound_candidates_remain_visible():
    run_id = uuid4()
    rule = {"field_key": "holes", "unit": "个", "value_kind": "actual",
            "dimension_schema": {"workface": {"required": True}}}
    groups = [{"value": value, "unit": "个", "value_kind": "actual", "source_role": "report_body",
               "dimensions": {"workface": {"value": "A"}}, "evidence_ids": ["e"]}
              for value in ["120", "173"]]
    field = SimpleNamespace(parse_run_id=run_id, field_key="holes", field_name="孔数", field_value=None,
                            confidence=None, validation_status="multiple_values",
                            extraction_detail={"value_groups": groups,
                                               "evidence_registry": {"e": {"page_number": 1}}})
    runs = [SimpleNamespace(parse_run_id=run_id, checklist_type="gas", extraction_profile={"rules": [rule]})]
    db = Mock(execute=AsyncMock(side_effect=[_result(runs), _result([field])]))
    item = (await stats.field_quality(checklist_type=None, limit=30, db=db))["items"][0]
    assert item["candidate_found"] == 1
    assert item["fact_count"] == 2
    assert item["risk_count"] == 1
    assert item["available_for_downstream"] == 1  # 同时提供事实及风险，不代替业务选择。
    assert item["review_needed"] == 1


@pytest.mark.parametrize("changes", [
    {"coverage": {"unvisited_pages": [2]}},
    {"failures": ["budget_exhausted"]},
    {"source_role": "unknown"},
    {"value_kind": "unknown"},
    {"dimensions": {}},
    {"unit": "米"},
    {"value": "not a number"},
])
def test_incomplete_or_unbound_fact_is_found_but_not_counted_as_structurally_available(changes):
    rule = {"unit": "个", "data_type": "integer", "value_kind": "actual",
            "strategy": {"agent_extract": {"identity_dimensions": ["workface"]}}}
    group = {"value": "0", "unit": "个", "value_kind": "actual", "source_role": "report_body",
             "dimensions": {"workface": {"value": "A"}}, "evidence_ids": ["e"]}
    detail = {"value_groups": [group], "evidence_registry": {"e": {"page_number": 1}}}
    for key, value in changes.items():
        (detail if key in {"coverage", "failures"} else group)[key] = value
    field = SimpleNamespace(field_value=None, validation_status="review_required", extraction_detail=detail)
    result = stats._field_inventory(field, rule)
    assert result["candidate_found"] == 1
    assert result["available_for_downstream"] == 0
    assert result["risk_count"] == 1


def test_group_count_does_not_double_count_repeated_occurrences():
    group = {"value": "0", "unit": "个", "value_kind": "actual", "source_role": "report_body",
             "dimensions": {"workface": {"value": "A"}}, "evidence_ids": ["a", "b"]}
    field = SimpleNamespace(field_value=None, validation_status="review_required",
                            instances=[{"value": "0"}, {"value": "0"}],
                            extraction_detail={"value_groups": [group], "evidence_registry": {
                                "a": {"page_number": 1}, "b": {"page_number": 2}}})
    result = stats._field_inventory(field, {"data_type": "integer", "unit": "个"})
    assert result["candidate_found"] == 1
    assert result["fact_count"] == result["value_group_count"] == 1
    assert result["available_for_downstream"] == 1
    assert result["risk_count"] == 0


@pytest.mark.asyncio
async def test_historical_structured_facts_do_not_invent_availability_without_a_snapshot():
    run_id = uuid4()
    field = SimpleNamespace(parse_run_id=run_id, field_key="holes", field_name="孔数", required=True,
                            field_value=None, confidence=None, validation_status="review_required",
                            extraction_detail={"value_groups": [{"value": "120", "unit": "个",
                                "value_kind": "actual", "source_role": "report_body", "evidence_ids": ["e"]}],
                                "evidence_registry": {"e": {"page_number": 1}}})
    runs = [SimpleNamespace(parse_run_id=run_id, checklist_type="gas", extraction_profile=None)]
    db = Mock(execute=AsyncMock(side_effect=[_result(runs), _result([field])]))
    item = (await stats.field_quality(checklist_type=None, limit=30, db=db))["items"][0]
    assert item["candidate_found"] == 1
    assert item["coverage"] is None
    assert item["available_for_downstream"] is None
    assert item["availability_unknown"] == 1
