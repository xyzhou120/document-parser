"""清单代理快照、持久化和提取连接的集成测试。"""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

from app.models.contracts import ExtractedField
from app.rules.spec import RuleSpec
from app.services.parse_service import ParseService, rule_snapshot


def test_agent_rule_snapshot_is_hashed_and_contains_no_secret():
    rule = RuleSpec(
        field_key="gas", field_name_cn="瓦斯", strategy={"agent_extract": {"background": "context"}}
    )
    snapshot = rule_snapshot([rule], "gas", "checklist_agent")
    assert snapshot["extraction_engine"] == "checklist_agent"
    assert len(snapshot["knowledge_hash"]) == 64
    assert (
        snapshot["knowledge_hash"]
        != rule_snapshot(
            [RuleSpec(field_key="gas", field_name_cn="瓦斯")], "gas", "checklist_agent"
        )["knowledge_hash"]
    )
    assert "api_key" not in snapshot.get("agent_model", {})
    assert "knowledge_hash" in snapshot


def test_save_results_agent_uses_unverified_confidence_and_observed_unit():
    service = ParseService.__new__(ParseService)
    db = Mock(execute=AsyncMock(), flush=AsyncMock())
    document_id, parse_run_id = uuid4(), uuid4()
    rule = RuleSpec(
        field_key="content",
        field_name_cn="含量",
        data_type="float",
        unit="m³/t",
        strategy={"agent_extract": {}},
        version=7,
    )
    field = ExtractedField(
        field_key="content", field_value="2.01", extraction_method="agent_extract", confidence=0.0
    )
    details = [
        {
            "field_key": "content",
            "field_name": "含量",
            "data_type": "float",
            "unit": "m³/t",
            "required": True,
            "rule_version": 7,
            "instances": [],
            "validation_status": "review_required",
        }
    ]
    asyncio.run(service._save_results(db, document_id, parse_run_id, [field], details, [rule], []))
    saved = db.add.call_args.args[0]
    assert saved.confidence is None
    assert saved.unit == "m³/t"
    assert saved.rule_version == 7
    assert saved.validation_status == "review_required"


def test_extract_freezes_agent_model_budget_and_render_profile(monkeypatch, tmp_path):
    captured = {}

    class FakeImages:
        def __init__(self, document, pages, workspace, pdf_path=None, dpi=None, region_dpi=None):
            captured["images"] = {
                "document": document,
                "pages": pages,
                "workspace": workspace,
                "pdf_path": pdf_path,
                "dpi": dpi,
                "region_dpi": region_dpi,
            }
            self.get = AsyncMock(return_value=b"image")
            self.receipts = {"evidence-key": {"cache_status": "MISS"}}

    class FakeAgent:
        def __init__(self, settings_override=None, model_config=None):
            captured["agent"] = {"settings": settings_override, "model": model_config}

        async def extract_all(self, pages, rules, image_provider):
            captured["extract"] = {"pages": pages, "rules": rules, "provider": image_provider}
            return (
                [],
                [{"field_key": r.field_key, "validation_status": "unresolved"} for r in rules],
                {"status": "UNRESOLVED"},
            )

    model = SimpleNamespace(
        model_name="vision-frozen",
        model_version="2026.10.08",
        base_url="http://gateway/v1",
        api_key="configured",
        timeout=17,
    )
    agent_settings = SimpleNamespace(
        max_rounds=4,
        max_view_pages=9,
        max_total_tokens=1234,
        field_batch_size=2,
        max_region_views=5,
        render_dpi=222,
    )
    agent_settings.model_dump = lambda: {
        "max_rounds": 4,
        "max_view_pages": 9,
        "max_total_tokens": 1234,
        "field_batch_size": 2,
        "max_region_views": 5,
        "render_dpi": 222,
    }
    monkeypatch.setattr(
        "app.services.parse_service.settings",
        SimpleNamespace(agent=agent_settings, models={"vision_llm": model}),
    )
    monkeypatch.setattr("app.services.parse_service.EvidenceImages", FakeImages)
    monkeypatch.setattr("app.agents.checklist.ChecklistAgent", FakeAgent)
    service = ParseService.__new__(ParseService)
    document = SimpleNamespace(document_id=uuid4(), file_hash="f" * 64)
    profile = {
        "agent_config": {"max_rounds": 8, "max_view_pages": 12, "render_dpi": 333},
        "agent_model": {
            "model_name": "snap-model",
            "model_version": "snap-v1",
            "base_url": "http://snap/v1",
            "timeout": 31,
        },
    }
    pages = [{"page_number": 1, "width": 100, "height": 100, "text": ""}]
    rules = [RuleSpec(field_key="gas", field_name_cn="瓦斯", strategy={"agent_extract": {}})]
    fields, details, runtime = asyncio.run(
        service._extract(pages, rules, "checklist_agent", document, Path(tmp_path), profile)
    )
    assert fields == []
    assert details[0]["validation_status"] == "unresolved"
    assert captured["images"]["dpi"] == 333
    assert captured["agent"]["settings"].max_rounds == 8
    assert captured["agent"]["settings"].max_view_pages == 12
    assert captured["agent"]["model"]["model_name"] == "snap-model"
    assert captured["agent"]["model"]["model_version"] == "snap-v1"
    assert captured["agent"]["model"]["timeout"] == 31
    assert runtime["image_artifacts"]["evidence-key"]["cache_status"] == "MISS"
