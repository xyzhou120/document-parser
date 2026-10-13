"""方案 B 检查（2026-10-10）：校准估算、可配置的历史图像，
推理开关、状态语义和原因码。

使用与 test_agent_checklist.py 相同的模拟客户端模式，不访问网络。
"""

import asyncio
import base64
import json
import math
from types import SimpleNamespace

from app.agents.checklist import ChecklistAgent
from app.rules.spec import RuleSpec


class FakeClient:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []
        self.chat = self
        self.completions = self

    async def create(self, **kwargs):
        self.calls.append(kwargs)
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


def run(responses, limits):
    async def provider(page_number, bbox):
        return b"image-bytes"

    client = FakeClient(responses)
    agent = ChecklistAgent(
        settings_override=limits,
        client=client,
        model_config={"model_name": "test", "model_version": "v1"},
    )
    result = asyncio.run(agent.extract_all(PAGES, [rule()], provider))
    return result, client


def _png_data_url(width, height):
    header = (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR"
              + width.to_bytes(4, "big") + height.to_bytes(4, "big"))
    return "data:image/png;base64," + base64.b64encode(header + b"\x00" * 16).decode()


# --- B1：校准调用前估算 -------------------------------------------------

def test_estimate_uses_measured_constants():
    agent = ChecklistAgent(settings_override={"token_per_char": 0.82, "image_tokens_per_kpx": 1.0})
    messages = [
        {"role": "system", "content": "s" * 1000},
        {"role": "user", "content": [
            {"type": "text", "text": "t" * 500},
            {"type": "image_url", "image_url": {"url": _png_data_url(1000, 1500)}},
        ]},
    ]
    # 1000 和 500 个字符各按 0.82 估算，加上一张 1000x1500 的 PNG，
    # 图像按每 1000 像素 1 token 估算。
    assert agent._estimate_tokens(messages) == math.ceil(1000 * 0.82 + 500 * 0.82 + 1500)


def test_unreadable_image_falls_back_to_fixed_estimate():
    agent = ChecklistAgent(settings_override={})
    assert agent._image_tokens("data:image/png;base64,AAAA") == 1500.0


# --- B2：历史图像 ----------------------------------------------------------------

def test_history_keeps_configured_number_of_images():
    agent = ChecklistAgent(settings_override={"history_images": 2})
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "task"}]
    for index in range(4):
        messages.append({"role": "user", "content": [
            {"type": "text", "text": f"r{index}"},
            {"type": "image_url", "image_url": {"url": _png_data_url(10, 10)}},
        ]})
    kept = agent._bounded_history(messages)
    images = [m for m in kept if isinstance(m["content"], list)]
    assert [m["content"][0]["text"] for m in images] == ["r2", "r3"]


def test_plan_b_defaults():
    limits = ChecklistAgent(settings_override={}).limits
    assert limits.history_images == 2
    assert limits.thinking_enabled is True
    assert limits.max_tokens == 6000
    # 方案 B 缺陷修复（2026-10-10）：实测初始系数为 0.55，运行时按调用校正。
    assert limits.token_per_char == 0.55
    assert limits.min_output_tokens == 2500
    assert limits.image_tokens_per_kpx == 1.0


def test_string_settings_are_parsed_by_type():
    limits = ChecklistAgent(settings_override={
        "thinking_enabled": "false", "token_per_char": "0.5", "history_images": "3",
    }).limits
    assert limits.thinking_enabled is False
    assert limits.token_per_char == 0.5
    assert limits.history_images == 3


# --- B3：推理开关和输出上限 -------------------------------------------------

def test_thinking_switch_is_sent_only_when_disabled():
    responses = [{"actions": [{"type": "view_page", "page_number": 1}]}, {"fields": []}]
    result, client = run(responses, {"max_rounds": 5, "thinking_enabled": False})
    assert all(call.get("extra_body") == {"chat_template_kwargs": {"enable_thinking": False}}
               for call in client.calls)
    fields, details, runtime = result
    assert [t["thinking"] for t in runtime["trace"] if t.get("event") == "model_response"] == [False, False]

    result, client = run(responses, {"max_rounds": 5})
    assert all("extra_body" not in call for call in client.calls)
    fields, details, runtime = result
    assert [t["thinking"] for t in runtime["trace"] if t.get("event") == "model_response"] == [True, True]


def test_trace_records_estimate_and_images_for_calibration():
    result, _ = run([{"actions": [{"type": "view_page", "page_number": 1}]}, {"fields": []}],
                    {"max_rounds": 5})
    _, _, runtime = result
    calls = [t for t in runtime["trace"] if t.get("event") == "model_response"]
    assert all(isinstance(t["estimate"], int) and t["estimate"] > 0 for t in calls)
    assert calls[0]["image_count"] == 0
    assert calls[1]["image_count"] == 1
    assert calls[0]["max_tokens"] == min(6000, calls[0]["max_tokens"])


# --- B5：状态语义和原因码 ------------------------------------------------

def test_field_status_matrix():
    assert ChecklistAgent._field_status(True, True) == "review_required"
    assert ChecklistAgent._field_status(True, False) == "partial_candidates"
    assert ChecklistAgent._field_status(False, True) == "not_found"
    assert ChecklistAgent._field_status(False, False) == "incomplete"


def test_candidate_without_finished_search_is_partial():
    # 第 1 轮查看第 1 页；第 2 轮提交候选但继续搜索，
    # 批次在完成前耗尽轮次。
    result, _ = run(
        [
            {"actions": [{"type": "view_page", "page_number": 1}]},
            {"fields": [value()], "actions": [{"type": "view_page", "page_number": 2}]},
        ],
        {"max_rounds": 5, "max_rounds_per_batch": 2},
    )
    fields, details, runtime = result
    assert details[0]["instances"]
    assert details[0]["validation_status"] == "partial_candidates"
    assert "no_finish_within_rounds" in details[0]["incomplete_reasons"]


def test_search_finished_without_candidate_is_not_found():
    result, _ = run(
        [{"actions": [{"type": "view_page", "page_number": 1}]}, {"fields": []}],
        {"max_rounds": 5},
    )
    fields, details, runtime = result
    assert not details[0]["instances"]
    assert details[0]["validation_status"] == "not_found"
    assert details[0]["incomplete_reasons"] == []
    assert details[0]["visited_page_count"] == 1


def test_unfinished_search_without_candidate_is_incomplete():
    result, _ = run(
        [{"actions": [{"type": "view_page", "page_number": 1}]}],
        {"max_rounds": 5, "max_rounds_per_batch": 1},
    )
    fields, details, runtime = result
    assert details[0]["validation_status"] == "incomplete"
    assert "no_finish_within_rounds" in details[0]["incomplete_reasons"]


def test_reason_codes_are_stable_and_sorted():
    codes = ChecklistAgent._reason_codes([
        "batch_token_budget_exhausted:0",
        "batch_unresolved:0",
        "model_response_truncated",
        "tool_actions_truncated",
    ])
    assert codes == ["actions_truncated", "no_finish_within_rounds",
                     "output_truncated", "token_budget_exhausted"]
