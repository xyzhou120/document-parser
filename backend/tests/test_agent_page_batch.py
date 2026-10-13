"""Full traversal and shared field calls, without claiming real model accuracy."""

import json
from types import SimpleNamespace

import pytest

from app.agents.checklist import ChecklistAgent
from app.rules.spec import RuleSpec


class PageClient:
    def __init__(self, *, hallucinate=False, fail_page=None):
        self.calls = []
        self.chat = SimpleNamespace(completions=self)
        self.hallucinate, self.fail_page = hallucinate, fail_page

    async def create(self, **kwargs):
        payload = json.loads(kwargs["messages"][-1]["content"])
        self.calls.append(payload)
        if payload["page_number"] == self.fail_page:
            raise TimeoutError()
        facts = []
        for rule in payload["rules"]:
            facts.append({"field_key": rule["field_key"], "value": "99" if self.hallucinate else "1.23",
                "quote": "报告正文 实测压力 1.23MPa", "unit": "MPa", "value_kind": "measured",
                "source_role": "report_body", "dimensions": {}})
        response = {"decisions": [{"candidate_id": payload["candidate_id"], "judgement": "accepted", "facts": facts}]}
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop",
            message=SimpleNamespace(content=json.dumps(response)))],
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=100, total_tokens=200))


@pytest.mark.asyncio
async def test_last_page_and_all_fields_share_one_call_per_page():
    pages = [{"page_number": i, "text": "报告正文 实测压力 1.23MPa"} for i in range(1, 4)]
    rules = [RuleSpec(field_key=f"pressure{i}", field_name_cn="压力", data_type="float", unit="MPa") for i in range(12)]
    client = PageClient()
    agent = ChecklistAgent(settings_override={"agent_mode": "page_batch"}, client=client)
    _, details, runtime = await agent.extract_all(pages, rules, None)
    assert len(client.calls) == 3
    assert runtime["coverage"]["unvisited_pages"] == []
    assert len(details) == 12
    assert all({item["page_num"] for item in detail["instances"]} == {1, 2, 3} for detail in details)


@pytest.mark.asyncio
async def test_failed_page_is_not_counted_as_covered_and_previous_results_remain():
    client = PageClient(fail_page=2)
    agent = ChecklistAgent(settings_override={"agent_mode": "page_batch"}, client=client)
    _, details, runtime = await agent.extract_all(
        [{"page_number": i, "text": "报告正文 实测压力 1.23MPa"} for i in (1, 2, 3)],
        [RuleSpec(field_key="pressure", field_name_cn="压力", data_type="float")], None)
    assert runtime["status"] == "PARTIAL"
    assert runtime["coverage"]["unvisited_pages"] == [2]
    assert len(details[0]["instances"]) == 2


@pytest.mark.asyncio
async def test_hallucinated_value_cannot_use_a_real_quote_as_proof():
    agent = ChecklistAgent(settings_override={"agent_mode": "page_batch"}, client=PageClient(hallucinate=True))
    fields, details, runtime = await agent.extract_all(
        [{"page_number": 1, "text": "报告正文 实测压力 1.23MPa"}],
        [RuleSpec(field_key="pressure", field_name_cn="压力", data_type="float")], None)
    assert not fields
    assert not details[0]["instances"]
    assert "page_batch_unbound_value:1" in runtime["failures"]
