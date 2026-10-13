"""Page batching behavior, using real gateway/evidence checks and in-memory rows.

These tests do not claim PostgreSQL lock/fencing coverage; the existing scheduler
PostgreSQL suite owns that boundary.
"""

import json
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.models.database import Document, ExtractedFieldModel, ParseRun
from app.models.execution import ParseCandidate, ParseFieldJob
from app.rules.spec import RuleSpec
from app.services.call_ledger import MemoryCallLedger
from app.services.execution import LeaseToken
from app.services.model_gateway import ModelGateway
from app.services.reliable_field_executor import ReliableFieldExecutorConfig
from app.services.reliable_page_executor import ReliablePageExecutor
from app.services.service_circuit import MemoryCircuitStore, ServiceCircuit


class MemoryRows:
    def __init__(self, rows):
        self.rows = rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, model, identity, **kwargs):
        key = {Document: "document_id", ParseRun: "parse_run_id", ParseFieldJob: "job_id",
               ParseCandidate: "candidate_id"}[model]
        return next((r for r in self.rows if isinstance(r, model) and getattr(r, key) == identity), None)

    def _query(self, statement):
        model = statement.column_descriptions[0]["entity"]
        values = list(statement.compile().params.items())
        rows = [r for r in self.rows if isinstance(r, model)]
        for key, value in values:
            attr = key.rsplit("_", 1)[0]
            if hasattr(model, attr):
                rows = [r for r in rows if getattr(r, attr) == value]
        return rows

    async def execute(self, statement):
        rows = self._query(statement)
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))

    async def scalar(self, statement):
        rows = self._query(statement)
        return rows[0] if rows else None

    def add(self, row):
        self.rows.append(row)

    async def flush(self):
        pass


class PageProvider:
    def __init__(self, mode="facts"):
        self.mode, self.calls, self.prompts = mode, 0, []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self, **kwargs):
        self.calls += 1
        payload = json.loads(kwargs["messages"][-1]["content"][0]["text"])
        self.prompts.append(payload)
        candidate = payload["candidates"][0]
        if candidate.get("stage") == "original_image_transcription":
            content = json.dumps({"decisions": [{"candidate_id": candidate["candidate_id"],
                "judgement": "accepted", "transcription": "报告正文 瓦斯压力 1.23MPa 实测"}]})
        elif self.mode == "invalid":
            content = "invalid JSON"
        else:
            facts = [{"field_key": rule["field_key"], "value": "1.23", "unit": "MPa",
                "quote": "报告正文 瓦斯压力 1.23MPa 实测",
                "value_kind": "measured", "source_role": "report_body", "dimensions": {},
                "evidence_ids": candidate["evidence_ids"]}
                for rule in candidate["rules"]] if self.mode == "facts" else []
            content = json.dumps({"decisions": [{"candidate_id": candidate["candidate_id"],
                "judgement": "accepted" if facts else "rejected", "facts": facts}]})
        return {"choices": [{"finish_reason": "stop", "message": {"content": content}}],
            "usage": {"prompt_tokens": 40, "completion_tokens": 20, "total_tokens": 60}}


def setup_executor(count=12, page_count=2, mode="facts", scopes=None, source="native"):
    document_id, run_id, revision = uuid4(), uuid4(), uuid4()
    rules = [RuleSpec(field_key=f"f{i:02}", field_name_cn="瓦斯压力", data_type="float", unit="MPa",
        value_kind="measured", strategy={"semantic_contract": {"scope": scopes}} if scopes else {})
        for i in range(count)]
    jobs = [ParseFieldJob(job_id=uuid4(), run_id=run_id, revision_id=revision,
        field_key=rule.field_key, rule_hash=rule.rule_semantic_hash,
        checkpoint={"rule_snapshot": asdict(rule)}, execution_state="queued", semantic_state="unknown")
        for rule in rules]
    run = ParseRun(parse_run_id=run_id, document_id=document_id, content_revision_id=revision,
        extraction_profile={"extraction_strategy": "page_batch_v1", "coordinator_field_key": "f00",
            "hard_limit_tokens": 400000, "max_run_seconds": 300})
    rows = MemoryRows([run, *jobs, Document(document_id=document_id)])
    provider = PageProvider(mode)
    gateway = ModelGateway(provider, MemoryCallLedger(), ServiceCircuit(MemoryCircuitStore()))
    executor = ReliablePageExecutor(lambda: rows, gateway, ReliableFieldExecutorConfig("test", "test"))
    pages = [{"page_number": i + 1, "text_blocks": [{"block_id": f"p{i + 1}", "source": source,
        "text": "报告正文 瓦斯压力 1.23MPa 实测"}]} for i in range(page_count)]
    pinned = {"pages": pages, "coverage_complete": True, "material_provenance": {}}
    executor._revision_content = AsyncMock(return_value=pinned)
    token = LeaseToken(run_id, jobs[0].job_id, "test", 0, 1)
    return executor, rows, jobs, provider, token


async def process(executor, rows, jobs, token, checkpoint=None):
    cp = deepcopy(checkpoint or jobs[0].checkpoint)
    for _ in range(12):
        outcome = await executor(token, cp)
        await executor.write_result(rows, jobs[0], outcome)
        cp = outcome.checkpoint
        jobs[0].checkpoint = cp
        if outcome.state in {"done", "failed_terminal"}:
            return outcome
    raise AssertionError("page batch did not terminate")


@pytest.mark.asyncio
async def test_all_fields_share_each_page_call_and_validated_results():
    executor, rows, jobs, provider, token = setup_executor()
    outcome = await process(executor, rows, jobs, token)
    assert outcome.state == "done"
    assert provider.calls == 2
    fields = [r for r in rows.rows if isinstance(r, ExtractedFieldModel)]
    assert len(fields) == 12
    assert all(f.instances and all(fact["usable"] for fact in f.instances) for f in fields)
    assert all(j.execution_state == "done" for j in jobs[1:])
    assert outcome.checkpoint["page_progress"]["processed_pages"] == 2


@pytest.mark.asyncio
async def test_restart_at_durable_page_cursor_does_not_repeat_completed_pages():
    executor, rows, jobs, provider, token = setup_executor()
    first = await executor(token, jobs[0].checkpoint)
    await executor.write_result(rows, jobs[0], first)
    assert provider.calls == 1
    assert all(j.execution_state == "queued" for j in jobs[1:])
    outcome = await process(executor, rows, jobs, replace(token, fence_token=2), first.checkpoint)
    assert outcome.state == "done" and provider.calls == 2


@pytest.mark.asyncio
async def test_invalid_page_cannot_become_not_found_or_leave_queued_siblings():
    executor, rows, jobs, provider, token = setup_executor(mode="invalid")
    outcome = await process(executor, rows, jobs, token)
    assert outcome.state == "failed_terminal"
    assert all(j.execution_state == "failed_terminal" for j in jobs[1:])
    assert all(j.semantic_state != "not_found" for j in jobs)
    assert outcome.checkpoint["failed_units"]


@pytest.mark.asyncio
async def test_field_explicit_scope_excludes_other_pages():
    executor, rows, jobs, provider, token = setup_executor(scopes={"mode": "explicit_ranges", "reason": "test scope",
        "ranges": [{"start": 2, "end": 2}]})
    outcome = await process(executor, rows, jobs, token)
    assert outcome.state == "done"
    candidates = [r for r in rows.rows if isinstance(r, ParseCandidate)]
    assert all(r.block_ref["page"] == 2 for r in candidates)


@pytest.mark.asyncio
async def test_deadline_preserves_processed_facts_and_terminates_all_siblings():
    executor, rows, jobs, provider, token = setup_executor()
    first = await executor(token, jobs[0].checkpoint)
    await executor.write_result(rows, jobs[0], first)
    first.checkpoint["page_started_at"] = (datetime.utcnow() - timedelta(seconds=301)).isoformat()
    outcome = await process(executor, rows, jobs, token, first.checkpoint)
    assert outcome.state == "failed_terminal"
    assert provider.calls == 1
    assert all(r.instances for r in rows.rows if isinstance(r, ExtractedFieldModel))
    assert all(j.execution_state == "failed_terminal" for j in jobs[1:])


@pytest.mark.asyncio
async def test_ocr_without_image_provider_is_explicit_failure():
    executor, rows, jobs, provider, token = setup_executor(source="ocr")
    outcome = await process(executor, rows, jobs, token)
    assert outcome.state == "failed_terminal" and provider.calls == 0
    assert outcome.checkpoint["page_progress"]["failed_units"] == 2


@pytest.mark.asyncio
async def test_ocr_transcription_is_shared_across_fields_and_fact_receipts_are_bound():
    import io

    from PIL import Image

    executor, rows, jobs, provider, token = setup_executor(source="ocr", page_count=1)
    data = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(data, format="PNG")
    executor.image_provider = AsyncMock(return_value=(data.getvalue(), {"bbox": [0, 0, 8, 8]}))
    outcome = await process(executor, rows, jobs, token)
    assert outcome.state == "done"
    assert provider.calls == 2  # extraction + one independent transcription for 12 fields
    assert executor.image_provider.await_count == 1
    fields = [r for r in rows.rows if isinstance(r, ExtractedFieldModel)]
    assert all(field.instances[0]["usable"] for field in fields)
    assert all(any(eid.startswith("image:") for eid in field.instances[0]["evidence_ids"])
        for field in fields)


@pytest.mark.asyncio
async def test_context_overflow_splits_rule_groups_without_repeating_unit():
    executor, rows, jobs, provider, token = setup_executor(count=12, page_count=1)
    original = executor.gateway.execute
    seen = []
    async def overflow_once(request):
        seen.append(request.candidates[0].payload.get("unit_id"))
        if len(seen) == 1:
            from app.services.model_gateway import GatewayOutcome
            return GatewayOutcome("context_overflow", pending_ids=[request.candidates[0].candidate_id])
        return await original(request)
    executor.gateway.execute = overflow_once
    outcome = await process(executor, rows, jobs, token)
    assert outcome.state == "done"
    assert len(provider.prompts) == 2
    assert len(set(seen)) == 3
    assert outcome.checkpoint["split_history"]


@pytest.mark.asyncio
async def test_gateway_timeout_fails_current_unit_and_keeps_prior_results(monkeypatch):
    executor, rows, jobs, provider, token = setup_executor(count=2, page_count=2)
    first = await executor(token, jobs[0].checkpoint)
    await executor.write_result(rows, jobs[0], first)
    async def blocked(_request):
        import asyncio
        await asyncio.sleep(0.05)
    executor.gateway.execute = blocked
    first.checkpoint["page_started_at"] = datetime.utcnow().isoformat()
    first.checkpoint["page_index"] = 1
    jobs[0].checkpoint = first.checkpoint
    monkeypatch.setattr("app.services.reliable_page_executor._remaining_seconds", lambda *_: 0.01)
    outcome = await executor(token, first.checkpoint)
    assert outcome.state == "failed_terminal"
    assert outcome.checkpoint["failed_units"]
    assert outcome.checkpoint["deadline_error"] == "page_batch_deadline_exceeded"


@pytest.mark.asyncio
async def test_length_splits_single_field_text_without_repeating_successful_child():
    executor, rows, jobs, provider, token = setup_executor(count=1, page_count=1, mode="rejected")
    executor._revision_content.return_value["pages"][0]["text_blocks"][0]["text"] = "第一段说明\n第二段说明"
    original = executor.gateway.execute
    seen = []
    async def length_once(request):
        seen.append(request.candidates[0].payload["unit_id"])
        if len(seen) == 1:
            from app.services.model_gateway import GatewayOutcome
            return GatewayOutcome("length", pending_ids=[request.candidates[0].candidate_id])
        return await original(request)
    executor.gateway.execute = length_once
    first = await executor(token, jobs[0].checkpoint)
    assert first.state == "yielded" and len(first.checkpoint["page_units"]) == 2
    await executor.write_result(rows, jobs[0], first)
    second = await executor(token, first.checkpoint)
    await executor.write_result(rows, jobs[0], second)
    outcome = await process(executor, rows, jobs, replace(token, fence_token=2), second.checkpoint)
    assert outcome.state == "done" and len(seen) == len(set(seen)) == 3
    assert provider.calls == 2
    assert "第一段说明" in outcome.checkpoint["page_units"][1]["context"]


@pytest.mark.asyncio
async def test_invalid_response_has_only_one_retry():
    executor, rows, jobs, provider, token = setup_executor(count=1, page_count=1, mode="invalid")
    outcome = await process(executor, rows, jobs, token)
    assert outcome.state == "failed_terminal" and provider.calls == 2


@pytest.mark.asyncio
async def test_split_depth_limit_fails_boundedly_instead_of_expanding_forever():
    executor, rows, jobs, provider, token = setup_executor(count=1, page_count=1)
    from app.services.model_gateway import GatewayOutcome
    executor.gateway.execute = AsyncMock(return_value=GatewayOutcome("context_overflow"))
    cp = deepcopy(jobs[0].checkpoint)
    from app.services.page_extraction import build_page_units
    cp["page_units"] = build_page_units(executor._revision_content.return_value["pages"])
    cp["split_depths"] = {cp["page_units"][0]["unit_id"]: 6}
    outcome = await process(executor, rows, jobs, token, cp)
    assert outcome.state == "failed_terminal"
    assert executor.gateway.execute.await_count == 1
