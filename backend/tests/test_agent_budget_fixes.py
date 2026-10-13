"""方案 B 缺陷修复（2026-10-10）。

1. 调用前估算根据网关实际用量校正，从而跟随提供方，而不是使用高估文本约 1.4 至 1.5 倍的固定常量。
2. 答案上限不会低于 min_output_tokens。剩余预算无法同时支付提示和该下限时，
   预算检查会拒绝调用，不再用很小的上限发送请求后让模型截断答案。

使用与其他代理测试相同的模拟客户端模式，不访问网络。
"""

import asyncio
import json
import math
from types import SimpleNamespace

from app.agents.checklist import ChecklistAgent
from app.rules.spec import RuleSpec


class UsageClient:
    """报告固定的提示 token 数，使校准结果可精确核对。"""

    def __init__(self, responses, prompt_tokens=None, completion_tokens=50):
        self.responses = iter(responses)
        self.calls = []
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.chat = self
        self.completions = self

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        value = next(self.responses)
        prompt = self.prompt_tokens
        if prompt is None:
            prompt = 100
        usage = SimpleNamespace(
            prompt_tokens=prompt,
            completion_tokens=self.completion_tokens,
            total_tokens=prompt + self.completion_tokens,
        )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(value)),
                                     finish_reason="stop")],
            usage=usage,
        )


PAGES = [
    {"page_number": 1, "width": 100, "height": 200, "text": "残余瓦斯含量 2.01 m³/t"},
]


def rule():
    return RuleSpec(field_key="gas", field_name_cn="含量", data_type="float", unit="m³/t",
                    strategy={"agent_extract": {}}, multi_instance=True)


def run(client, settings):
    async def provider(page_number, bbox):
        return b"image-bytes"

    agent = ChecklistAgent(settings_override=settings, client=client,
                           model_config={"model_name": "test", "model_version": "v1"})
    return agent, asyncio.run(agent.extract_all(PAGES, [rule()], provider))


# --- 缺陷 2：输出下限 -----------------------------------------------------------------

def test_answer_cap_is_never_below_the_floor_when_a_call_is_made():
    client = UsageClient([{"actions": [{"type": "view_page", "page_number": 1}]}, {"fields": []}])
    _, (fields, details, runtime) = run(client, {"max_rounds": 5, "max_total_tokens": 60000})
    caps = [call["max_tokens"] for call in client.calls]
    assert caps and all(cap >= 2500 for cap in caps)


def test_small_remaining_budget_refuses_the_call_instead_of_truncating_it():
    # 初始提示已占数百 token，因此 2600 token 的预算在计入提示后
    # 不足 2500 token 的答案下限：不应发起付费调用。
    client = UsageClient([{"fields": []}])
    _, (fields, details, runtime) = run(client, {"max_rounds": 5, "max_total_tokens": 2600})
    assert client.calls == []
    assert "token_budget_exhausted" in runtime["failures"]
    assert not [t for t in runtime["trace"] if t.get("event") == "model_response"]


def test_cap_is_at_least_the_floor_when_a_call_is_made_near_the_limit():
    agent = ChecklistAgent(settings_override={"min_output_tokens": 2500, "max_tokens": 6000})
    result = asyncio.run(agent._model_call(
        UsageClient([{"fields": []}]), "m", [{"role": "user", "content": "x" * 100}],
        room=2600, runtime={"token_usage": {"prompt_tokens": 0, "completion_tokens": 0,
                                             "total_tokens": 0}, "trace": []},
        trace={}))
    assert result["status"] == "ok"
    assert result["max_tokens"] >= 2500


# --- 缺陷 1：校准估算 ----------------------------------------------------------

def test_estimate_starts_from_measured_factor_and_tracks_real_usage():
    agent = ChecklistAgent(settings_override={})
    messages = [{"role": "user", "content": "字" * 1000}]
    assert agent._estimate_tokens(messages) == math.ceil(1000 * agent._token_per_char)
    assert agent._token_per_char == 0.55
    # 提供方报告的提示 token 数远低于初始系数预测值。
    agent._observe_usage(messages, {"prompt_tokens": 300, "completion_tokens": 10,
                                    "total_tokens": 310})
    assert agent._token_per_char < 0.55
    assert agent._estimate_tokens(messages) < 550


def test_calibration_is_clamped_and_ignores_short_prompts():
    agent = ChecklistAgent(settings_override={})
    short = [{"role": "user", "content": "短" * 50}]
    agent._observe_usage(short, {"prompt_tokens": 10_000})
    assert agent._token_per_char == 0.55
    long = [{"role": "user", "content": "字" * 1000}]
    for _ in range(20):
        agent._observe_usage(long, {"prompt_tokens": 10_000_000})
    assert agent._token_per_char == 1.5
    for _ in range(40):
        agent._observe_usage(long, {"prompt_tokens": 1})
    assert agent._token_per_char == 0.2


def test_images_are_not_absorbed_into_the_text_factor():
    agent = ChecklistAgent(settings_override={})
    image_only_part = {"type": "image_url", "image_url": {"url": "data:image/png;base64,"}}
    messages = [{"role": "user", "content": [{"type": "text", "text": "字" * 500}, image_only_part]}]
    text_chars, image_tokens = agent._message_sizes(messages)
    assert text_chars == 500
    assert image_tokens == 1500.0
    before = agent._token_per_char
    agent._observe_usage(messages, {"prompt_tokens": 500 * before + 1500})
    assert abs(agent._token_per_char - before) < 1e-9


def test_trace_records_calibration_for_later_review():
    client = UsageClient([{"actions": [{"type": "view_page", "page_number": 1}]}, {"fields": []}],
                         prompt_tokens=800)
    _, (fields, details, runtime) = run(client, {"max_rounds": 5})
    calls = [t for t in runtime["trace"] if t.get("event") == "model_response"]
    assert all("calibration" in t for t in calls)
