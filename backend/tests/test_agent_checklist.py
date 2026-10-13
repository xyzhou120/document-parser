"""测试证据和预算边界，不宣称模型准确率。"""

import asyncio
import json
from types import SimpleNamespace

from app.agents.checklist import ChecklistAgent
from app.rules.spec import RuleSpec


class FakeClient:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.messages = []
        self.chat = self
        self.completions = self

    async def create(self, **kwargs):
        self.messages.append(kwargs)
        value = next(self.responses)
        if isinstance(value, Exception):
            raise value
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(value)))],
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=50, total_tokens=150),
        )


PAGES = [
    {"page_number": 1, "width": 100, "height": 200, "text": "2.01 m²/t"},
    {"page_number": 2, "width": 100, "height": 200, "text": ""},
]


def rule(**kwargs):
    return RuleSpec(
        field_key="gas",
        field_name_cn="含量",
        data_type="float",
        unit="m³/t",
        strategy={"agent_extract": {}},
        **kwargs,
    )


def value(value="2.01", evidence_ids=None, dimensions=None, **kwargs):
    return {
        "field_key": "gas",
        "value": value,
        "raw_value": value,
        "quote": value + " m³/t",
        "evidence_ids": evidence_ids or ["ev-000001"],
        "unit": "m³/t",
        "value_kind": "measured",
        "source_role": "report",
        "dimensions": dimensions or {},
        **kwargs,
    }


def run(responses, rules=None, limits=None):
    seen = []

    async def provider(page_number, bbox):
        seen.append((page_number, bbox))
        return b"image-bytes"

    client = FakeClient(responses)
    agent = ChecklistAgent(
        settings_override=limits or {"max_rounds": 5},
        client=client,
        model_config={"model_name": "test", "model_version": "v1"},
    )
    result = asyncio.run(agent.extract_all(PAGES, rules or [rule()], provider))
    return result, seen, client


def test_viewed_evidence_retained_despite_ocr_disagreement():
    (fields, details, runtime), seen, _ = run(
        [
            {"actions": [{"type": "view_page", "page_number": 1}]},
            {"fields": [value()]},
        ]
    )
    assert seen == [(1, None)]
    assert fields[0].field_value == "2.01"
    assert fields[0].extraction_method == "agent_extract"
    assert details[0]["instances"][0]["quality_issues"][0]["code"] == "OCR_DISAGREEMENT"
    assert details[0]["review_status"] == "UNVERIFIED"
    assert details[0]["instances"][0]["bbox"] is None
    assert runtime["coverage"]["unvisited_pages"] == [2]


def test_fake_evidence_is_rejected():
    (fields, details, runtime), _, _ = run([{"fields": [value(evidence_ids=["made-up"])]}])
    assert not fields
    assert not details[0]["instances"]
    assert "invalid_evidence_reference" in runtime["failures"]


def test_table_structure_tool_is_a_navigation_hint_not_viewed_evidence():
    agent = ChecklistAgent(settings_override={"max_rounds": 2})
    pages = [{"page_number": 1, "width": 100, "height": 200,
              "tables": [{"table_id": "p1-t1", "rows": [["孔数"], ["120"]], "cells": []}]}]
    visited, registry, runtime = {}, {}, {"failures": []}
    async def provider(page, bbox):
        raise AssertionError("Structure inspection must not invoke image rendering")
    result, image = asyncio.run(agent._run_action({"type": "inspect_tables", "page_number": 1},
        pages, {1: pages[0]}, visited, registry, provider, runtime))
    assert result["tables"][0]["table_id"] == "p1-t1"
    assert image is None and visited == {} and registry == {}


def test_out_of_bounds_nonfinite_and_unknown_tools_never_render():
    (fields, _, _), seen, _ = run(
        [
            {
                "actions": [
                    {"type": "view_region", "page_number": 1, "bbox": [0, 0, 101, 10]},
                    {"type": "view_region", "page_number": 1, "bbox": [0, 0, float("nan"), 10]},
                    {"type": "shell", "page_number": 1},
                ]
            },
            {"fields": []},
        ]
    )
    assert not fields
    assert seen == []


def test_region_evidence_has_platform_coordinates_not_model_character_box():
    (_, details, _), seen, _ = run(
        [
            {"actions": [{"type": "view_region", "page_number": 2, "bbox": [10, 20, 90, 190]}]},
            {"fields": [value()]},
        ]
    )
    assert seen == [(2, [10, 20, 90, 190])]
    instance = details[0]["instances"][0]
    assert instance["page_num"] == 2
    assert instance["bbox"] == {"x0": 10, "y0": 20, "x1": 90, "y1": 190}
    assert instance["location_status"] == "region"


def test_unknown_required_identity_prevents_same_value_grouping():
    (_, details, _), _, _ = run(
        [
            {"actions": [{"type": "view_page", "page_number": 1}]},
            {"fields": [value("2.010"), value("2.01")]},
        ],
        rules=[rule(dimension_schema={"workface": {"required": True}})],
    )
    assert len(details[0]["value_groups"]) == 2
    assert details[0]["merged_value"] is None


def test_known_identity_groups_exact_decimal_and_preserves_all_evidence():
    dims = {"workface": {"value": "40203", "evidence_ids": ["ev-000001"], "quote": "40203工作面"}}
    (_, details, _), _, _ = run(
        [
            {
                "actions": [
                    {"type": "view_page", "page_number": 1},
                    {"type": "view_page", "page_number": 2},
                ]
            },
            {
                "fields": [
                    value("2.010", dimensions=dims),
                    value("2.01", evidence_ids=["ev-000002"], dimensions=dims),
                ]
            },
        ],
        rules=[rule(dimension_schema={"workface": {"required": True}})],
    )
    groups = details[0]["value_groups"]
    assert len(groups) == 1
    assert groups[0]["evidence_ids"] == ["ev-000001", "ev-000002"]
    assert len(groups[0]["raw_values"]) == 2
    assert groups[0]["identity"] == {"workface": "40203"}


def test_total_round_budget_is_not_reset_for_field_batches():
    other = RuleSpec(field_key="other", field_name_cn="其他", strategy={"agent_extract": {}})
    (_, details, runtime), _, client = run(
        [{"fields": []}], rules=[rule(), other], limits={"max_rounds": 1, "field_batch_size": 1}
    )
    assert len(client.messages) == 1
    assert len(details) == 2
    assert "round_budget_exhausted" in runtime["failures"]


def test_page_budget_prevents_new_page_views():
    (_, _, _), seen, _ = run(
        [
            {
                "actions": [
                    {"type": "view_page", "page_number": 1},
                    {"type": "view_page", "page_number": 2},
                ]
            },
            {"fields": []},
        ],
        limits={"max_rounds": 3, "max_view_pages": 1},
    )
    assert seen == [(1, None)]


def test_remote_error_is_unresolved_never_fallback():
    (fields, details, runtime), _, _ = run([RuntimeError("unavailable")])
    assert not fields
    # 方案 B：远程错误后的未完成搜索应标记为 incomplete，不能使用回退值。
    assert details[0]["validation_status"] == "incomplete"
    assert "model_error:RuntimeError" in details[0]["incomplete_reasons"]
    assert runtime["status"] == "UNRESOLVED"
    assert runtime["failures"][0] == "model_error:RuntimeError"


def test_bool_coordinates_and_page_are_rejected():
    (_, _, _), seen, _ = run(
        [
            {
                "actions": [
                    {"type": "view_page", "page_number": True},
                    {"type": "view_region", "page_number": 1, "bbox": [False, 0, 10, 20]},
                ]
            },
            {"fields": []},
        ]
    )
    assert seen == []


def test_missing_model_configuration_still_returns_rule_details():
    agent = ChecklistAgent(settings_override={"models": {}})
    fields, details, runtime = asyncio.run(agent.extract_all(PAGES, [rule()], None))
    assert not fields
    assert details[0]["field_key"] == "gas"
    assert details[0]["validation_status"] == "incomplete"
    assert "vision_llm_not_configured" in details[0]["incomplete_reasons"]
    assert runtime["status"] == "UNRESOLVED"


def test_unvisited_pages_make_completion_partial():
    (_, details, runtime), _, _ = run(
        [
            {"actions": [{"type": "view_page", "page_number": 1}]},
            {"fields": [value()]},
        ]
    )
    assert runtime["status"] == "PARTIAL"
    assert details[0]["coverage"]["unvisited_pages"] == [2]


def test_source_roles_are_not_merged():
    (_, details, _), _, _ = run(
        [
            {"actions": [{"type": "view_page", "page_number": 1}]},
            {"fields": [dict(value(), source_role="report"), dict(value(), source_role="design")]},
        ]
    )
    assert len(details[0]["value_groups"]) == 2


def test_too_large_knowledge_reports_unresolved_instead_of_truncated_json():
    r = rule()
    r.strategy["agent_extract"]["background"] = "背景" * 10000
    (_, details, runtime), _, client = run([], rules=[r], limits={"max_context_chars": 1000})
    assert not client.messages
    assert details[0]["validation_status"] == "incomplete"
    assert "context_budget_exhausted" in details[0]["incomplete_reasons"]
    assert runtime["failures"][0].startswith("knowledge_context_budget_exhausted")


def test_gateway_missing_usage_is_accounted_conservatively():
    class NoUsageClient(FakeClient):
        async def create(self, **kwargs):
            response = await super().create(**kwargs)
            response.usage = None
            return response

    async def provider(page, bbox):
        return b"img"

    client = NoUsageClient(
        [{"actions": [{"type": "view_page", "page_number": 1}]}, {"fields": [value()]}]
    )
    agent = ChecklistAgent(settings_override={"max_rounds": 3}, client=client)
    _, _, runtime = asyncio.run(agent.extract_all(PAGES, [rule()], provider))
    assert runtime["usage_estimated"] is True
    assert runtime["token_usage"]["total_tokens"] > 2000


def test_extra_actions_are_reported():
    (_, _, runtime), _, _ = run(
        [
            {"actions": [{"type": "list_pages"}] * 2},
            {"fields": []},
        ],
        limits={"field_batch_size": 1, "max_actions_per_round": 1},
    )
    assert "tool_actions_truncated" in runtime["failures"]


def test_internally_created_client_is_closed_and_base_normalized(monkeypatch):
    client = FakeClient([{"fields": []}])
    client.closed = False

    async def close():
        client.closed = True

    client.close = close
    calls = []

    def make_client(**kwargs):
        calls.append(kwargs)
        return client

    monkeypatch.setattr("app.agents.checklist.AsyncOpenAI", make_client)
    agent = ChecklistAgent(
        settings_override={},
        model_config={"base_url": "localhost:8000", "model_name": "test", "timeout": 20},
    )
    asyncio.run(agent.extract_all(PAGES, [rule()], None))
    assert client.closed
    assert calls[0]["base_url"] == "http://localhost:8000/v1"
    assert calls[0]["max_retries"] == 0
    assert calls[0]["timeout"] == 20


def test_truncated_model_response_never_becomes_candidates():
    class TruncatedClient(FakeClient):
        async def create(self, **kwargs):
            response = await super().create(**kwargs)
            response.choices[0].finish_reason = "length"
            return response

    client = TruncatedClient([{"fields": [value()]}])
    agent = ChecklistAgent(settings_override={}, client=client)
    fields, details, runtime = asyncio.run(agent.extract_all(PAGES, [rule()], None))
    assert not fields
    assert not details[0]["instances"]
    assert "model_response_truncated" in runtime["failures"]
    response_trace = [item for item in runtime["trace"] if item.get("event") == "model_response"]
    assert response_trace[0]["finish_reason"] == "length"


def test_truncated_response_accounts_actual_gateway_usage_before_diagnostic():
    class TruncatedClient(FakeClient):
        async def create(self, **kwargs):
            response = await super().create(**kwargs)
            response.choices[0].finish_reason = "length"
            response.usage.prompt_tokens = 321
            response.usage.completion_tokens = 777
            response.usage.total_tokens = 1098
            return response

    client = TruncatedClient([{"fields": []}])
    agent = ChecklistAgent(settings_override={}, client=client)
    _, _, runtime = asyncio.run(agent.extract_all(PAGES, [rule()], None))
    assert runtime["token_usage"] == {
        "prompt_tokens": 321,
        "completion_tokens": 777,
        "total_tokens": 1098,
    }
    assert runtime["failures"].count("model_response_truncated") == 1
    assert runtime["trace"][0]["usage"]["total_tokens"] == 1098


def test_dimension_only_evidence_refs_are_registered():
    dims = {"workface": {"value": "40203", "evidence_ids": ["ev-000002"], "quote": "40203"}}
    (_, details, _), _, _ = run(
        [
            {
                "actions": [
                    {"type": "view_page", "page_number": 1},
                    {"type": "view_page", "page_number": 2},
                ]
            },
            {"fields": [value(dimensions=dims)]},
        ],
        rules=[rule(dimension_schema={"workface": {"required": True}})],
    )
    assert "ev-000002" in details[0]["evidence_registry"]


def test_unknown_source_roles_are_not_merged():
    (_, details, _), _, _ = run(
        [
            {"actions": [{"type": "view_page", "page_number": 1},
                          {"type": "view_page", "page_number": 2}]},
            {
                "fields": [
                    dict(value(), source_role="unknown"),
                    dict(value(evidence_ids=["ev-000002"]), source_role="unknown"),
                ]
            },
        ]
    )
    assert len(details[0]["value_groups"]) == 2


def test_missing_required_evidence_roles_numeric_and_valuekind_flagged():
    r = rule(value_kind="actual")
    r.strategy["agent_extract"]["evidence_requirements"] = ["value", "unit", "scope"]
    (_, details, _), _, _ = run(
        [
            {"actions": [{"type": "view_page", "page_number": 1}]},
            {"fields": [value("补 补", evidence_roles={"ev-000001": ["value"]})]},
        ],
        rules=[r],
    )
    candidate = details[0]["instances"][0]
    codes = {issue["code"] for issue in candidate["quality_issues"]}
    assert "INVALID_NUMERIC_VALUE" in codes
    assert "VALUE_KIND_MISMATCH" in codes
    assert "EVIDENCE_ROLE_MISSING" in codes
    assert candidate["value"] == "补 补"
    assert candidate["evidence_roles"] == {"ev-000001": ["value"]}
    assert details[0]["unit"] == "m³/t"


def test_empty_finish_without_visual_evidence_requires_another_tool_turn():
    (_, details, runtime), seen, client = run(
        [
            {"finish": True},
            {"actions": [{"type": "view_page", "page_number": 2}]},
            {"fields": []},
        ]
    )
    assert len(client.messages) == 3
    assert seen == [(2, None)]
    # 方案 B：搜索结束且未找到值时为 not_found，不是 unresolved。
    assert details[0]["validation_status"] == "not_found"
    assert details[0]["visited_page_count"] == 1
    assert runtime["status"] == "UNRESOLVED"
    assert any(
        item.get("event") == "finish_rejected_no_visual_evidence" for item in runtime["trace"]
    )


def test_repeated_empty_finishes_stop_at_budget_and_never_mean_absent():
    (_, details, runtime), seen, client = run([{"fields": []}] * 2, limits={"max_rounds": 2})
    assert len(client.messages) == 2
    assert not seen
    # 方案 B：没有视觉证据时，多次空结果也不能代表“不存在”。
    assert details[0]["validation_status"] == "incomplete"
    assert details[0]["incomplete_reasons"] == ["no_finish_within_rounds"]
    assert runtime["status"] == "UNRESOLVED"
    assert "batch_unresolved:0" in runtime["failures"]


def test_progressive_candidates_survive_later_model_failure():
    (_, details, runtime), seen, _ = run([
        {"actions": [{"type": "view_page", "page_number": 1}]},
        {"fields": [value()], "finish": False,
         "actions": [{"type": "view_page", "page_number": 2}]},
        TimeoutError("failure"),
    ])
    assert seen == [(1, None), (2, None)]
    assert details[0]["instances"][0]["value"] == "2.01"
    assert runtime["status"] == "PARTIAL"
    assert "model_error:TimeoutError" in runtime["failures"]


def test_each_batch_can_submit_candidates_then_request_more_evidence():
    (_, details, _), seen, _ = run([
        {"actions": [{"type": "view_page", "page_number": 1}]},
        {"fields": [value()], "finish": False,
         "actions": [{"type": "view_page", "page_number": 2}]},
        {"finish": True},
    ], limits={"field_batch_size": 1})
    assert len(seen) == 2
    assert details[0]["instance_count"] == 1


def test_page_map_is_small_and_knowledge_constraints_are_preserved():
    agent = ChecklistAgent(settings_override={})
    r = rule()
    instructions = "必须保留的条件" * 400
    r.strategy["agent_extract"]["instructions"] = instructions
    payload = json.loads(agent._initial_prompt([
        {"page_number": i, "width": 100, "height": 200, "text": "正文" * 2000}
        for i in range(1, 38)
    ], [r]))
    assert max(len(p["text"]) for p in payload["pages"]) <= 41
    assert payload["rules"][0]["knowledge"]["instructions"] == instructions


def test_empty_candidate_list_does_not_skip_requested_visual_actions():
    (_, details, _), seen, _ = run([
        {"fields": [], "finish": False,
         "actions": [{"type": "view_page", "page_number": 1}]},
        {"fields": [value()], "finish": True},
    ])
    assert seen == [(1, None)]
    assert details[0]["instances"][0]["value"] == "2.01"


def test_finish_with_requested_actions_runs_tools_before_finishing():
    (_, details, _), seen, _ = run([
        {"fields": [], "finish": True,
         "actions": [{"type": "view_page", "page_number": 1}]},
        {"fields": [value()], "finish": True},
    ])
    assert seen == [(1, None)]
    assert details[0]["instance_count"] == 1


def test_first_batch_cannot_starve_later_field_of_rounds():
    other = RuleSpec(field_key="other", field_name_cn="其他", strategy={"agent_extract": {}})
    (_, details, runtime), _, client = run([
        {"actions": [{"type": "list_pages"}]},
        {"actions": [{"type": "list_pages"}]},
        {"actions": [{"type": "view_page", "page_number": 1}]},
        {"fields": [dict(value(), field_key="other")]},
    ], rules=[rule(), other], limits={"max_rounds": 4, "field_batch_size": 1})
    assert len(client.messages) == 4
    assert details[0]["instances"] == []
    assert details[1]["instances"][0]["value"] == "2.01"
    assert runtime["rounds_used"] == 4
    assert [b["round_allowance"] for b in runtime["batch_budgets"]] == [2, 2]


def test_native_shortlist_is_navigation_without_candidate_evidence():
    r = rule()
    r.strategy["agent_extract"]["synonyms"] = ["施工钻孔"]
    agent = ChecklistAgent(settings_override={})
    payload = json.loads(agent._initial_prompt([
        {"page_number": 1, "text": "目录"},
        {"page_number": 2, "text": "共施工1239个钻孔"},
        {"page_number": 3, "text": ""},
    ], [r]))
    hits = payload["retrieval_shortlist"]["gas"]
    assert hits[0]["page_number"] == 2
    assert "evidence_id" not in hits[0]
    assert "value" not in hits[0]
    assert hits[0]["matched_hints"] == []  # 数字打断了精确短语。


def test_tool_action_limit_is_independent_of_field_batch_size():
    (_, _, runtime), seen, _ = run([
        {"actions": [{"type": "view_page", "page_number": 1},
                     {"type": "view_page", "page_number": 2}]},
        {"fields": []},
    ], limits={"field_batch_size": 1, "max_actions_per_round": 2})
    assert seen == [(1, None), (2, None)]
    assert "tool_actions_truncated" not in runtime["failures"]


def test_progressive_resubmission_is_not_a_new_occurrence():
    (_, details, runtime), _, _ = run([
        {"actions": [{"type": "view_page", "page_number": 1}]},
        {"fields": [value()], "finish": False},
        {"fields": [value()], "finish": True},
    ])
    assert details[0]["instance_count"] == 1
    assert runtime["resubmitted_candidates"] == 1


def test_resubmission_dedup_keeps_different_unknown_evidence():
    (_, details, _), _, _ = run([
        {"actions": [{"type": "view_page", "page_number": 1},
                     {"type": "view_page", "page_number": 2}]},
        {"fields": [value(), value(evidence_ids=["ev-000002"])], "finish": True},
    ], rules=[rule(dimension_schema={"workface": {"required": True}})])
    assert details[0]["instance_count"] == 2
    assert len(details[0]["value_groups"]) == 2


def test_source_role_outside_configured_vocabulary_is_flagged_not_remapped():
    r = rule()
    r.strategy["agent_extract"]["source_roles"] = ["report_body", "report_sheet"]
    (_, details, _), _, _ = run([
        {"actions": [{"type": "view_page", "page_number": 1}]},
        {"fields": [value(source_role="report")]},
    ], rules=[r])
    candidate = details[0]["instances"][0]
    assert candidate["source_role"] == "report"
    assert "SOURCE_ROLE_UNCONFIGURED" in {i["code"] for i in candidate["quality_issues"]}
