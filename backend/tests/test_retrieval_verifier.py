"""Plan C3-C5 checks: verification of deterministic candidates, platform acceptance, per-field budgets.

The fake model reads the candidate ids it is shown and answers from their text, so each test
exercises the platform rules (acceptance, evidence, image policy, budgets) rather than model
accuracy.  No network, no OCR, no database.
"""

import asyncio
import json
from types import SimpleNamespace

from app.agents.checklist import ChecklistAgent
from app.rules.spec import RuleSpec


def png(width=200, height=100):
    header = (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR"
              + width.to_bytes(4, "big") + height.to_bytes(4, "big"))
    return header + b"\x00" * 16


class ScriptedClient:
    """Answers each request with ``respond(payload)``; records every request."""

    def __init__(self, respond, prompt_tokens=800, completion_tokens=150):
        self.respond = respond
        self.calls = []
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.chat = self
        self.completions = self

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        user = kwargs["messages"][-1]["content"]
        payload = json.loads(next(p["text"] for p in user if p["type"] == "text"))
        content = self.respond(payload)
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        usage = SimpleNamespace(prompt_tokens=self.prompt_tokens,
                                completion_tokens=self.completion_tokens,
                                total_tokens=self.prompt_tokens + self.completion_tokens)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason="stop")],
            usage=usage,
        )


def answer_if_contains(needle, value, unit="m³/t"):
    """A model that accepts exactly the candidates whose text contains ``needle``."""
    def respond(payload):
        rows = []
        for cand in payload["candidates"]:
            if needle in cand["text"]:
                rows.append({"id": cand["id"], "is_target": True, "value": value, "unit": unit,
                             "raw": value, "value_kind": "measured", "source_role": "report_body"})
            else:
                rows.append({"id": cand["id"], "is_target": False})
        return {"results": rows}
    return respond


def rule(field_key="res", name="残余瓦斯含量实测值", unit="m³/t", data_type="float",
         synonyms=("残余瓦斯含量",), multi=True, dims=None):
    schema = dims or {}
    return RuleSpec(
        field_key=field_key, field_name_cn=name, unit=unit, data_type=data_type,
        multi_instance=multi, dimension_schema=schema,
        strategy={"agent_extract": {"synonyms": list(synonyms),
                                    "identity_dimensions": list(schema),
                                    "background": "测试背景"}},
    )


def native_page(number, *lines, ocr_status="not_needed", tables=None):
    blocks = []
    for index, text in enumerate(lines):
        y = 100 + index * 20
        blocks.append({"text": text, "bbox": [50, y, 320, y + 10], "source": "native"})
    return {"page_number": number, "width": 600, "height": 800,
            "text": "\n".join(lines), "text_blocks": blocks, "tables": tables or [],
            "image_count": 0, "ocr_status": ocr_status}


def ocr_page(number, text, bbox, label="text", ocr_status="ok"):
    return {"page_number": number, "width": 600, "height": 800, "text": text,
            "text_blocks": [{"text": text, "bbox": bbox, "source": "ocr", "label": label}],
            "tables": [], "image_count": 1, "ocr_status": ocr_status}


def settings(**overrides):
    base = {"agent_mode": "retrieval", "max_rounds": 60}
    base.update(overrides)
    return base


async def image_provider(page_number, bbox):
    return png()


def run(pages, rules, client, **overrides):
    agent = ChecklistAgent(settings_override=settings(**overrides), client=client,
                           model_config={"model_name": "test", "model_version": "v1"})
    return asyncio.run(agent.extract_all(pages, rules, image_provider))


# --- acceptance rules ------------------------------------------------------------------------

def test_native_strong_candidate_is_accepted_from_text_without_an_image():
    client = ScriptedClient(answer_if_contains("2.23", "2.23"))
    pages = [native_page(10, "残余瓦斯含量 2.23 m³/t")]
    fields, details, runtime = run(pages, [rule()], client)
    detail = details[0]
    assert detail["validation_status"] == "review_required"
    instance = detail["instances"][0]
    assert instance["value"] == "2.23" and instance["unit"] == "m³/t"
    assert instance["image_checked"] is False
    assert instance["quote"] == "残余瓦斯含量 2.23 m³/t"
    assert instance["evidence_ids"] and runtime["evidence_registry"][instance["evidence_ids"][0]]["scope"] == "text"
    # One text-only request: no image part was sent for an unambiguous native window.
    content = client.calls[0]["messages"][-1]["content"]
    assert [p["type"] for p in content] == ["text"]
    assert runtime["status"] == "COMPLETED"


def test_ocr_candidate_is_accepted_only_with_an_attached_crop():
    client = ScriptedClient(answer_if_contains("2.23", "2.23"))
    pages = [ocr_page(21, "残余瓦斯含量(m3/t)：2.23", [293, 300, 604, 320])]
    fields, details, runtime = run(pages, [rule()], client)
    instance = details[0]["instances"][0]
    assert instance["image_checked"] is True
    assert len(instance["evidence_ids"]) == 2
    roles = instance["evidence_roles"]
    image_ev = runtime["evidence_registry"][instance["evidence_ids"][1]]
    assert image_ev["scope"] == "region" and image_ev["bbox"] is not None
    codes = {i["code"] for i in instance["quality_issues"]}
    assert {"VISUAL_CHECKED", "OCR_TEXT_SOURCE"} <= codes
    assert set(roles) == set(instance["evidence_ids"])
    content = client.calls[0]["messages"][-1]["content"]
    assert [p["type"] for p in content] == ["text", "text", "image_url"]


def test_six_ocr_crops_are_split_across_calls_never_dropped_for_image_count():
    client = ScriptedClient(answer_if_contains("2.23", "2.23"))
    pages = [ocr_page(i, "残余瓦斯含量 2.23 m³/t", [50, 100, 300, 110]) for i in range(1, 7)]
    # Six distinct OCR crops with a cap of four images per call: two calls, each within the cap.
    for index, page in enumerate(pages):
        page["text_blocks"][0]["bbox"] = [50, 100 + index * 30, 300, 110 + index * 30]
    fields, details, runtime = run(pages, [rule()], client, max_images_per_call=4)
    assert len(client.calls) == 2
    for call in client.calls:
        content = call["messages"][-1]["content"]
        assert sum(1 for p in content if p["type"] == "image_url") <= 4
    instances = details[0]["instances"]
    assert len(instances) == 6 and all(i["image_checked"] for i in instances)
    assert details[0]["validation_status"] == "review_required"
    assert "image_budget_exhausted" not in runtime["field_coverage"]["res"]["failures"]


def test_whole_page_images_are_limited_by_estimated_image_tokens():
    client = ScriptedClient(answer_if_contains("2.23", "2.23"))
    # Whole-page OCR (no box): each page image is about 1.3k tokens at 120 DPI for an A4-sized page,
    # so one call carries at most ~3 under a 4000-token image cap.
    pages = [{"page_number": i, "width": 595, "height": 842, "text": "残余瓦斯含量 2.23 m³/t",
              "text_blocks": [{"text": "残余瓦斯含量 2.23 m³/t", "bbox": None, "source": "ocr"}],
              "tables": [], "image_count": 1, "ocr_status": "ok"} for i in range(1, 5)]
    fields, details, runtime = run(pages, [rule()], client, max_image_tokens_per_call=4000,
                                   render_dpi=120)
    assert len(client.calls) == 2
    sizes = [sum(1 for p in call["messages"][-1]["content"] if p["type"] == "image_url")
             for call in client.calls]
    assert max(sizes) <= 3 and sum(sizes) == 4


def test_value_not_in_candidate_is_rejected_and_blocks_completion():
    def wrong_value(payload):
        return {"results": [{"id": c["id"], "is_target": True, "value": "2.24", "unit": "m³/t"}
                            for c in payload["candidates"]]}
    client = ScriptedClient(wrong_value)
    fields, details, runtime = run([native_page(10, "残余瓦斯含量 2.23 m³/t")], [rule()], client)
    assert details[0]["instances"] == []
    assert details[0]["validation_status"] == "incomplete"
    assert "value_not_in_candidate" in details[0]["incomplete_reasons"]
    assert fields == []


def test_threshold_only_window_is_judged_negative_and_the_search_finishes():
    client = ScriptedClient(lambda payload: {"results": [
        {"id": c["id"], "is_target": False} for c in payload["candidates"]]})
    pages = [native_page(4, "残余瓦斯含量限值不得超过 8 m³/t")]
    fields, details, runtime = run(pages, [rule()], client)
    assert details[0]["validation_status"] == "not_found"
    assert details[0]["incomplete_reasons"] == []
    assert runtime["retrieval"]["res"]["negative"] == 1
    assert runtime["status"] == "COMPLETED"


def test_threshold_judged_as_a_value_is_accepted_only_with_the_hint_kept_for_review():
    def model_claims_threshold(payload):
        return {"results": [{"id": c["id"], "is_target": True, "value": "8", "unit": "m³/t"}
                            for c in payload["candidates"]]}
    client = ScriptedClient(model_claims_threshold)
    fields, details, runtime = run([native_page(4, "残余瓦斯含量限值不得超过 8 m³/t")], [rule()], client)
    # "8" is in the text, so the platform accepts the model's claim as written, but the threshold
    # hint stays on the instance so a reviewer can see why it was flagged.
    instance = details[0]["instances"][0]
    assert "threshold_context" in instance["candidate_hints"]


# --- failures and reasons ---------------------------------------------------------------------

def test_unreadable_page_makes_an_empty_search_incomplete_not_absent():
    client = ScriptedClient(lambda payload: {"results": []})
    pages = [{"page_number": 9, "width": 600, "height": 800, "text": "", "text_blocks": [],
              "tables": [], "image_count": 1, "ocr_status": "failed"}]
    fields, details, runtime = run(pages, [rule()], client)
    assert details[0]["validation_status"] == "incomplete"
    assert details[0]["incomplete_reasons"] == ["ocr_unavailable_pages"]
    assert client.calls == []


def test_field_budget_refuses_the_call_and_names_the_reason():
    client = ScriptedClient(answer_if_contains("2.23", "2.23"))
    fields, details, runtime = run([native_page(10, "残余瓦斯含量 2.23 m³/t")], [rule()], client,
                                   field_token_budget=2600)
    assert client.calls == []
    assert details[0]["validation_status"] == "incomplete"
    assert details[0]["incomplete_reasons"] == ["token_budget_exhausted"]


def test_invalid_model_output_is_incomplete_with_reason():
    client = ScriptedClient(lambda payload: "这不是 JSON")
    fields, details, runtime = run([native_page(10, "残余瓦斯含量 2.23 m³/t")], [rule()], client)
    assert details[0]["validation_status"] == "incomplete"
    assert details[0]["incomplete_reasons"] == ["invalid_output"]


def test_image_failure_for_an_ocr_candidate_is_reported_not_faked():
    client = ScriptedClient(answer_if_contains("2.23", "2.23"))

    async def broken(page_number, bbox):
        raise RuntimeError("render failed")

    agent = ChecklistAgent(settings_override=settings(), client=client,
                           model_config={"model_name": "test", "model_version": "v1"})
    pages = [ocr_page(21, "残余瓦斯含量(m3/t)：2.23", [293, 300, 604, 320])]
    fields, details, runtime = asyncio.run(agent.extract_all(pages, [rule()], broken))
    assert details[0]["instances"] == []
    assert details[0]["validation_status"] == "incomplete"
    assert "image_unavailable" in details[0]["incomplete_reasons"]
    assert client.calls == []


def test_candidates_beyond_the_call_capacity_make_the_field_partial():
    client = ScriptedClient(answer_if_contains("残余瓦斯含量", "2.23"))
    lines = [f"残余瓦斯含量 2.{i:02d} m³/t" for i in range(20)]
    # The model accepts the candidates it is shown, each with the number printed in its own window.
    client.respond = lambda payload: {"results": [
        {"id": c["id"], "is_target": True, "value": c["numbers"][0], "unit": "m³/t"}
        for c in payload["candidates"]]}
    fields, details, runtime = run([native_page(3, *lines)], [rule()], client,
                                   candidates_per_call=5, max_calls_per_field=2)
    assert len(client.calls) == 2
    assert "candidates_truncated" in runtime["field_coverage"]["res"]["failures"]
    assert details[0]["validation_status"] == "partial_candidates"
    assert "candidates_truncated" in details[0]["incomplete_reasons"]


# --- per-field budgets ------------------------------------------------------------------------

def test_one_field_exhausting_its_budget_does_not_starve_another():
    many = [f"残余瓦斯含量 {i}.50 m³/t" for i in range(13)]
    other_page = native_page(20, "残余压力测定 0.20 MPa")
    heavy = native_page(3, *many)
    heavy_rule = rule(field_key="heavy", name="残余瓦斯含量实测值", synonyms=("残余瓦斯含量",))
    light_rule = rule(field_key="light", name="残余瓦斯压力", unit="MPa", synonyms=("残余压力",))

    def respond(payload):
        rows = []
        for cand in payload["candidates"]:
            hit = "0.20" in cand["text"] or cand["text"].startswith("残余瓦斯含量")
            rows.append({"id": cand["id"], "is_target": hit,
                         "value": "0.20" if "0.20" in cand["text"] else cand["numbers"][0],
                         "unit": "MPa" if "MPa" in cand["text"] else "m³/t"})
        return {"results": rows}

    # The first call reports a large prompt, so the heavy field's second call finds no room.
    client = ScriptedClient(respond, prompt_tokens=9000, completion_tokens=150)
    fields, details, runtime = run([heavy, other_page], [heavy_rule, light_rule], client)
    by_key = {d["field_key"]: d for d in details}
    assert "token_budget_exhausted" in runtime["field_coverage"]["heavy"]["failures"]
    assert by_key["light"]["instances"], "the second field must keep its own allowance"
    assert by_key["light"]["validation_status"] == "review_required"


def test_call_count_never_exceeds_the_per_field_limit():
    client = ScriptedClient(answer_if_contains("残余瓦斯含量", "2.23"))
    lines = [f"残余瓦斯含量 2.{i:02d} m³/t" for i in range(40)]
    fields, details, runtime = run([native_page(3, *lines)], [rule()], client,
                                   candidates_per_call=6, max_calls_per_field=3)
    assert len(client.calls) <= 3
    assert runtime["retrieval"]["res"]["calls"] == len(client.calls)


def test_retrieval_details_expose_candidate_counts_for_review():
    client = ScriptedClient(answer_if_contains("2.23", "2.23"))
    fields, details, runtime = run([native_page(10, "残余瓦斯含量 2.23 m³/t")], [rule()], client)
    retrieval = details[0]["retrieval"]
    assert retrieval["candidates_total"] == 1
    assert retrieval["accepted"] == 1
    assert retrieval["calls"] == 1
    assert runtime["engine"] == "checklist-retrieval-v2"


def test_legacy_mode_is_still_selected_when_configured():
    agent = ChecklistAgent(settings_override={"agent_mode": "legacy"})
    assert agent.limits.agent_mode == "legacy"
    assert ChecklistAgent(settings_override={}).limits.agent_mode == "legacy"
    assert ChecklistAgent(settings_override={"agent_mode": "bogus"}).limits.agent_mode == "legacy"
