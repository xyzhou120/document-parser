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
from app.models.execution import ParseCandidate, ParseFieldJob, ParseModelCall
from app.rules.spec import RuleSpec
from app.services.call_ledger import MemoryCallLedger
from app.services.execution import LeaseToken
from app.services.model_gateway import ModelGateway
from app.services.reliable_field_executor import ReliableFieldExecutorConfig
from app.services.reliable_page_executor import ReliablePageExecutor
from app.services.service_circuit import CircuitPolicy, MemoryCircuitStore, ServiceCircuit


class MemoryRows:
    def __init__(self, rows):
        self.rows = rows
        self.ledger = None

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
        if model is ParseModelCall and self.ledger is not None:
            rows.extend(ParseModelCall(**asdict(record)) for record in self.ledger.records.values())
        for key, value in values:
            attr = key.rsplit("_", 1)[0]
            if hasattr(model, attr):
                rows = [r for r in rows if (getattr(r, attr) in value if isinstance(value, list)
                                           else getattr(r, attr) == value)]
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
    rows.ledger = gateway.ledger
    executor = ReliablePageExecutor(lambda: rows, gateway, ReliableFieldExecutorConfig("test", "test"))
    pages = [{"page_number": i + 1, "text_blocks": [{"block_id": f"p{i + 1}", "source": source,
        "text": "报告正文 瓦斯压力 1.23MPa 实测"}]} for i in range(page_count)]
    pinned = {"pages": pages, "coverage_complete": True, "material_provenance": {}}
    executor._revision_content = AsyncMock(return_value=pinned)
    token = LeaseToken(run_id, jobs[0].job_id, "test", 0, 1)
    return executor, rows, jobs, provider, token


@pytest.mark.parametrize("stage", ["page", "image"])
@pytest.mark.parametrize("after_timeout", [False, True])
@pytest.mark.parametrize("old_checkpoint", [False, True])
async def test_circuit_wait_replays_same_attempt_and_preserves_paid_results(stage, after_timeout, old_checkpoint):
    executor, rows, jobs, provider, token = setup_executor(
        count=1, page_count=1, source="ocr" if stage == "image" else "native")
    if stage == "image":
        executor._revision_content.return_value["pages"][0]["text_blocks"][0]["label"] = "table"
        executor.image_provider = AsyncMock(return_value=(b"page-image", {"image_hash": "abc"}))
    now = [datetime(2026, 10, 16)]
    circuit = ServiceCircuit(MemoryCircuitStore(), CircuitPolicy(threshold=1, cooldown_seconds=10),
                             clock=lambda: now[0])
    executor.gateway.circuit = circuit
    executor.gateway.clock = lambda: now[0]
    original_execute = executor.gateway.execute
    original_create = provider.chat.completions.create
    target_first = True
    timeout_pending = after_timeout
    provider_attempts = []

    async def create(**kwargs):
        nonlocal timeout_pending
        provider_attempts.append(kwargs)
        payload = json.loads(kwargs["messages"][-1]["content"][0]["text"])
        image = payload["candidates"][0].get("stage") == "original_image_transcription"
        if timeout_pending and image == (stage == "image"):
            timeout_pending = False
            raise TimeoutError("synthetic timeout")
        return await original_create(**kwargs)

    async def execute(request):
        nonlocal target_first
        target = request.checkpoint_key.startswith("page-image:") == (stage == "image")
        if target and target_first:
            target_first = False
            if not after_timeout:
                permit = await circuit.acquire(request.provider_key)
                await circuit.failure(request.provider_key, permit)
        return await original_execute(request)

    provider.chat.completions.create = create
    executor.gateway.execute = execute
    retry_key, prior_key = (("image_retry", "image_prior_call_id") if stage == "image"
                            else ("page_retry", "prior_call_id"))
    first = await executor(token, jobs[0].checkpoint)
    assert first.state == "waiting_service"
    cp = first.checkpoint
    expected_prior = cp.get(prior_key)
    assert cp[retry_key] == int(after_timeout)
    if after_timeout:
        assert expected_prior is not None
        # 新 retry 仍被熔断拦住，没有发出第二个模型请求。
        first = await executor(replace(token, fence_token=2), cp)
        cp = first.checkpoint
    records = executor.gateway.ledger.records
    queued = [r for r in records.values() if r.state == "queued"]
    assert len(queued) == 1
    queued_id = queued[0].call_id
    assert (str(queued[0].retry_of_call_id) if queued[0].retry_of_call_id else None) == expected_prior
    if old_checkpoint:
        cp[prior_key] = str(queued_id)  # 现场旧版本保存的错误 lineage。
        if not after_timeout:
            # 旧代码在构建首次请求之后才向持久化 unit 添加此内部标记。
            cp["page_units"][0]["text_sufficient"] = False
    attempts_before = len(provider_attempts)
    for fence in (3, 4):
        waiting = await executor(replace(token, fence_token=fence), cp)
        assert waiting.state == "waiting_service"
        cp = waiting.checkpoint
        assert cp[retry_key] == int(after_timeout)
        assert cp.get(prior_key) == expected_prior
        assert len(provider_attempts) == attempts_before
        assert len([r for r in records.values() if r.state == "queued"]) == 1
    now[0] += timedelta(seconds=11)
    finished = await executor(replace(token, fence_token=5), cp)
    assert finished.state == "yielded"
    assert records[queued_id].state == "succeeded"
    assert len(provider_attempts) == (2 if stage == "image" else 1) + int(after_timeout)
    # 同一 checkpoint 重放已付费结果，提取和原图核验都不能重复发送。
    await executor(replace(token, fence_token=6), cp)
    assert len(provider_attempts) == (2 if stage == "image" else 1) + int(after_timeout)


@pytest.mark.parametrize("stage", ["page", "image"])
async def test_quota_resume_blocked_by_circuit_keeps_its_dispatch_identity(stage):
    from tests.test_model_gateway import HTTPError

    executor, rows, jobs, provider, token = setup_executor(
        count=1, page_count=1, source="ocr" if stage == "image" else "native")
    if stage == "image":
        executor._revision_content.return_value["pages"][0]["text_blocks"][0]["label"] = "table"
        executor.image_provider = AsyncMock(return_value=(b"page-image", {"image_hash": "abc"}))
    now = [datetime(2026, 10, 16)]
    circuit = ServiceCircuit(MemoryCircuitStore(), CircuitPolicy(threshold=1, cooldown_seconds=10),
                             clock=lambda: now[0])
    executor.gateway.circuit = circuit
    original = provider.chat.completions.create
    quota_pending = True

    async def create(**kwargs):
        nonlocal quota_pending
        payload = json.loads(kwargs["messages"][-1]["content"][0]["text"])
        image = payload["candidates"][0].get("stage") == "original_image_transcription"
        if quota_pending and image == (stage == "image"):
            quota_pending = False
            raise HTTPError(429, "insufficient_quota")
        return await original(**kwargs)

    provider.chat.completions.create = create
    first = await executor(token, jobs[0].checkpoint)
    assert first.state == "waiting_external_quota"
    cp = first.checkpoint
    quota_key = "image_external_quota" if stage == "image" else "external_quota"
    prior_key = "image_prior_call_id" if stage == "image" else "prior_call_id"
    prior = cp[prior_key]
    permit = await circuit.acquire("test")
    await circuit.failure("test", permit)
    for fence in (2, 3):
        waiting = await executor(replace(token, fence_token=fence), cp)
        assert waiting.state == "waiting_service"
        cp = waiting.checkpoint
        assert cp[quota_key] and cp[prior_key] == prior
        queued = [r for r in executor.gateway.ledger.records.values() if r.state == "queued"]
        assert len(queued) == 1 and queued[0].dispatch_key.endswith(":quota-resume")
    now[0] += timedelta(seconds=11)
    finished = await executor(replace(token, fence_token=4), cp)
    assert finished.state == "yielded"
    assert queued[0].state == "succeeded"


@pytest.mark.parametrize("after_timeout", [False, True])
async def test_field_batch_circuit_wait_preserves_retry_lineage(after_timeout):
    from app.services.reliable_field_executor import ReliableFieldExecutor, candidate_payload, snapshot_rule
    from tests.test_reliable_field_executor import FakeProvider

    page_executor, rows, jobs, _provider, token = setup_executor(count=1, page_count=1)
    now = [datetime(2026, 10, 16)]
    circuit = ServiceCircuit(MemoryCircuitStore(), CircuitPolicy(threshold=1, cooldown_seconds=10),
                             clock=lambda: now[0])
    provider = FakeProvider()
    original = provider.chat.completions.create
    timeout_pending = after_timeout

    async def create(**kwargs):
        nonlocal timeout_pending
        if timeout_pending:
            timeout_pending = False
            raise TimeoutError("synthetic timeout")
        return await original(**kwargs)

    provider.chat.completions.create = create
    gateway = ModelGateway(provider, rows.ledger, circuit, clock=lambda: now[0])
    executor = ReliableFieldExecutor(lambda: rows, gateway, page_executor.config)
    if not after_timeout:
        permit = await circuit.acquire("test")
        await circuit.failure("test", permit)
    candidate = ParseCandidate(candidate_id=uuid4(), job_id=token.job_id, stable_key="test",
        window="报告正文 瓦斯压力 1.23MPa 实测", context="", source="native", state="pending",
        block_ref={"page": 1}, region_ref={}, span_ref={"segments": []}, evidence={})
    rows.add(candidate)
    proposal = candidate_payload(candidate, jobs[0].revision_id)
    rule = snapshot_rule(jobs[0].checkpoint["rule_snapshot"])
    cp = {"revision_id": str(jobs[0].revision_id)}

    async def judge():
        return await executor._judge(token, cp, [proposal], rule, None, [], {})

    first = await judge()
    assert first.state == "waiting_service"
    if after_timeout:
        first = await judge()
        assert first.state == "waiting_service"
    batch = next(iter(cp["candidate_batches"].values()))
    payload = batch["payload"]
    prior = payload.get("prior_call_id")
    queued = next(r for r in rows.ledger.records.values() if r.state == "queued")
    payload["prior_call_id"] = str(queued.call_id)
    for _ in range(2):
        waiting = await judge()
        assert waiting.state == "waiting_service"
        payload = next(iter(cp["candidate_batches"].values()))["payload"]
        assert payload.get("prior_call_id") == prior
        assert payload["retry_seq"] == int(after_timeout)
    now[0] += timedelta(seconds=11)
    assert (await judge()).state == "yielded"
    assert queued.state == "succeeded" and provider.calls == 1


async def test_old_checkpoint_repair_still_rejects_changed_model_input():
    executor, rows, jobs, provider, token = setup_executor(count=1, page_count=1)
    now = datetime(2026, 10, 16)
    circuit = ServiceCircuit(MemoryCircuitStore(), CircuitPolicy(threshold=1), clock=lambda: now)
    executor.gateway.circuit = circuit
    permit = await circuit.acquire("test")
    await circuit.failure("test", permit)
    waiting = await executor(token, jobs[0].checkpoint)
    assert waiting.state == "waiting_service"
    cp = waiting.checkpoint
    queued = next(iter(rows.ledger.records.values()))
    cp["prior_call_id"] = str(queued.call_id)
    cp["page_units"][0]["text_sufficient"] = False
    cp["page_units"][0]["text"] += " 已改变的文档正文"
    with pytest.raises(ValueError, match="different model input"):
        await executor(replace(token, fence_token=2), cp)
    assert provider.calls == 0 and len(rows.ledger.records) == 1


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


async def test_bad_crop_isolated_to_page_and_other_pages_finish():
    from app.services.evidence_images import InvalidEvidenceRegion
    executor, rows, jobs, provider, token = setup_executor(count=1, page_count=2, source="ocr")
    for page in executor._revision_content.return_value["pages"]:
        page["text_blocks"][0]["label"] = "table"
    calls = []
    async def images(document, pages, candidate):
        calls.append(candidate["page_number"])
        if candidate["page_number"] == 1:
            raise InvalidEvidenceRegion("out of bounds")
        return b"original", {"image_hash": "hash"}
    executor.image_provider = images
    result = await process(executor, rows, jobs, token)
    assert result.state == "done" and result.semantic_state == "review_required"
    assert result.checkpoint["page_progress"]["processed_pages"] == 2
    assert len(result.checkpoint["failed_units"]) == 1
    assert 2 in calls and provider.calls > 0


async def test_unchanged_page_validation_is_reused_but_changed_source_is_rechecked(monkeypatch):
    from unittest.mock import Mock
    from app.services.reliable_page_executor import validate_candidate_decision
    executor, rows, jobs, provider, token = setup_executor(count=1, page_count=1)
    validate = Mock(wraps=validate_candidate_decision)
    monkeypatch.setattr("app.services.reliable_page_executor.validate_candidate_decision", validate)
    outcome = await executor(token, jobs[0].checkpoint)
    await executor.write_result(rows, jobs[0], outcome)
    first_count = validate.call_count
    assert first_count > 0
    first_facts = deepcopy(next(r for r in rows.rows if isinstance(r, ExtractedFieldModel)).instances)
    await executor.write_result(rows, jobs[0], outcome)
    assert validate.call_count == first_count
    assert next(r for r in rows.rows if isinstance(r, ExtractedFieldModel)).instances == first_facts
    candidate = next(r for r in rows.rows if isinstance(r, ParseCandidate))
    candidate.window = "已变化的来源文字"
    await executor.write_result(rows, jobs[0], outcome)
    assert validate.call_count > first_count


@pytest.mark.asyncio
def test_page_dispatch_key_follows_the_request_contract():
    from app.services.reliable_page_executor import _page_dispatch_key
    job = uuid4()
    assert _page_dispatch_key(job, "unit", 0, 0) == _page_dispatch_key(job, "unit", 0, 0)
    assert _page_dispatch_key(job, "unit", 0, 0) != f"{job}:page:unit:g0:0"


async def test_long_page_with_many_fields_splits_before_the_model():
    executor, _rows, jobs, provider, token = setup_executor(count=12, page_count=1)
    long_text = "报告正文 瓦斯压力 1.23MPa 实测。\n" * 80
    page = executor._revision_content.return_value["pages"][0]
    page["text_blocks"][0]["text"] = long_text
    first = await executor(token, jobs[0].checkpoint)
    assert first.state == "yielded"
    assert provider.calls == 0
    assert first.checkpoint["split_history"][0]["reason"] == "field_group"
    assert len(first.checkpoint["page_units"]) == 2
    assert all(len(unit["field_keys"]) <= 6 for unit in first.checkpoint["page_units"])


async def test_prefetch_uses_planned_children_and_leaves_no_parent_response():
    executor, rows, jobs, provider, token = setup_executor(count=12, page_count=2)
    for i, page in enumerate(executor._revision_content.return_value["pages"]):
        page["text_blocks"][0].update(text="报告正文 瓦斯压力 1.23MPa 实测。\n" * 80,
                                       source=f"source-{i}")
    planned = await executor(token, jobs[0].checkpoint)
    assert provider.calls == 0
    parents = {row["parent"] for row in planned.checkpoint["split_history"]}
    assert len(parents) == 2
    result = await process(executor, rows, jobs, token, planned.checkpoint)
    assert result.state == "done"
    assert provider.calls == 4
    assert not result.checkpoint["prefetched_responses"]
    assert not parents & {row.checkpoint_key.removeprefix("page:") for row in rows.ledger.records.values()}


async def test_old_paid_parent_prefetch_is_consumed_without_splitting_or_repeating():
    executor, rows, jobs, provider, token = setup_executor(count=12, page_count=1)
    from app.services.page_extraction import build_page_units
    page = executor._revision_content.return_value["pages"][0]
    page["text_blocks"][0]["text"] = "报告正文 瓦斯压力 1.23MPa 实测。\n" * 80
    unit = build_page_units([page])[0]
    cp = deepcopy(jobs[0].checkpoint)
    cp["page_units"] = [unit]
    cp["prefetched_responses"] = {unit["unit_id"]: {"state": "succeeded", "decisions": [
        {"candidate_id": "old", "judgement": "rejected"}]}}
    result = await process(executor, rows, jobs, token, cp)
    assert provider.calls == 0
    assert len(result.checkpoint["page_units"]) == 1
    assert not result.checkpoint.get("split_history")
    assert not result.checkpoint["prefetched_responses"]


async def test_old_dispatched_parent_replays_after_lost_checkpoint_without_new_children(monkeypatch):
    executor, rows, jobs, provider, token = setup_executor(count=12, page_count=1)
    executor._revision_content.return_value["pages"][0]["text_blocks"][0]["text"] = (
        "报告正文 瓦斯压力 1.23MPa 实测。\n" * 80)
    # 模拟旧版本把长父单元发出并写了账本，但检查点事务还没有提交。
    with monkeypatch.context() as old:
        old.setattr("app.services.reliable_page_executor._plan_field_splits",
                    lambda units, rules, scopes, checkpoint: units)
        paid = await executor(token, jobs[0].checkpoint)
    assert provider.calls == 1 and len(paid.checkpoint["page_units"]) == 1
    resumed = await executor(replace(token, fence_token=2), jobs[0].checkpoint)
    assert provider.calls == 1
    assert not resumed.checkpoint.get("split_history")
    assert len(resumed.checkpoint["page_units"]) == 1
    recovered = resumed.checkpoint["page_results"][0]["response"]
    original = paid.checkpoint["page_results"][0]["response"]
    assert recovered["state"] == original["state"] == "succeeded"
    assert recovered["decisions"] == original["decisions"]
    assert recovered["call_id"] == original["call_id"]


async def test_incremental_page_write_preserves_old_evidence_and_batches_summary():
    executor, rows, jobs, provider, token = setup_executor(count=1, page_count=6)
    executor.config = replace(executor.config, page_parallelism=1)
    for i, page in enumerate(executor._revision_content.return_value["pages"]):
        page["text_blocks"][0]["source"] = f"source-{i}"
    first = await executor(token, jobs[0].checkpoint)
    await executor.write_result(rows, jobs[0], first)
    field = next(row for row in rows.rows if isinstance(row, ExtractedFieldModel))
    detail = field.extraction_detail
    old_candidate = next(row for row in rows.rows if isinstance(row, ParseCandidate) and row.state == "accepted")
    old_evidence = old_candidate.evidence
    queries = []
    execute = rows.execute
    async def tracked(statement):
        queries.append(statement)
        return await execute(statement)
    rows.execute = tracked
    second = await executor(token, first.checkpoint)
    queries.clear()
    await executor.write_result(rows, jobs[0], second)
    assert field.extraction_detail is detail
    assert old_candidate.evidence is old_evidence
    candidate_queries = [q for q in queries if q.column_descriptions[0]["entity"] is ParseCandidate]
    assert len(candidate_queries) == 1
    assert "stable_key" in str(candidate_queries[0])
    assert len([r for r in rows.rows if isinstance(r, ParseCandidate) and r.state == "accepted"]) == 2
    # 一个新 executor（模拟重启）依赖已提交 checkpoint，而不是内存缓存。
    restarted = ReliablePageExecutor(lambda: rows, executor.gateway, executor.config)
    restarted._revision_content = executor._revision_content
    result = await process(restarted, rows, jobs, token, second.checkpoint)
    assert result.state == "done"
    assert len([r for r in rows.rows if isinstance(r, ParseCandidate) and r.state == "accepted"]) == 6
    assert field.instances
    assert provider.calls == 6


async def test_batched_and_full_each_page_publication_have_identical_final_facts_and_coverage():
    executor, rows, jobs, _provider, token = setup_executor(count=2, page_count=6)
    executor.config = replace(executor.config, page_parallelism=1)
    for i, page in enumerate(executor._revision_content.return_value["pages"]):
        page["text_blocks"][0]["source"] = f"source-{i}"
    baseline = MemoryRows(deepcopy(rows.rows))
    baseline_job = next(row for row in baseline.rows if isinstance(row, ParseFieldJob) and row.job_id == token.job_id)
    cp = jobs[0].checkpoint
    for _ in range(10):
        outcome = await executor(token, cp)
        full_outcome = replace(outcome, checkpoint=deepcopy(outcome.checkpoint))
        full_outcome.checkpoint.pop("page_write_sync", None)
        await executor.write_result(baseline, baseline_job, full_outcome)
        await executor.write_result(rows, jobs[0], outcome)
        cp = outcome.checkpoint
        jobs[0].checkpoint = cp
        if outcome.state == "done":
            break
    assert outcome.state == "done"
    final = {row.field_key: (row.instances, row.validation_status, row.extraction_detail)
             for row in rows.rows if isinstance(row, ExtractedFieldModel)}
    expected = {row.field_key: (row.instances, row.validation_status, row.extraction_detail)
                for row in baseline.rows if isinstance(row, ExtractedFieldModel)}
    assert final == expected


async def test_four_separate_pages_run_together():
    import asyncio
    executor, _rows, jobs, provider, token = setup_executor(count=1, page_count=4)
    for index, page in enumerate(executor._revision_content.return_value["pages"]):
        page["text_blocks"][0]["source"] = f"source-{index}"
    current = 0
    peak = 0
    original = executor.gateway.execute

    async def tracked(request):
        nonlocal current, peak
        current += 1
        peak = max(peak, current)
        try:
            await asyncio.sleep(0.02)
            return await original(request)
        finally:
            current -= 1

    executor.gateway.execute = tracked
    await executor(token, jobs[0].checkpoint)
    assert peak == 4
    assert provider.calls == 4


async def test_two_distinct_pages_are_sent_together():
    import asyncio
    executor, rows, jobs, provider, token = setup_executor(count=1, page_count=2)
    executor._revision_content.return_value["pages"][1]["text_blocks"][0]["source"] = "human_correction"
    current = 0
    peak = 0
    original = executor.gateway.execute

    async def tracked(request):
        nonlocal current, peak
        current += 1
        peak = max(peak, current)
        try:
            await asyncio.sleep(0.02)
            return await original(request)
        finally:
            current -= 1

    executor.gateway.execute = tracked
    await executor(token, jobs[0].checkpoint)
    assert peak == 2
    assert provider.calls == 2


@pytest.mark.asyncio
async def test_ocr_table_pages_are_sent_together_with_their_images():
    import asyncio
    executor, rows, jobs, provider, token = setup_executor(count=1, page_count=2, source="ocr")
    pages = executor._revision_content.return_value["pages"]
    for page in pages:
        page["text_blocks"][0]["label"] = "table"
    executor.image_provider = AsyncMock(return_value=(b"page-image", {"image_hash": "abc"}))
    current = 0
    peak = 0
    original = executor.gateway.execute

    async def tracked(request):
        nonlocal current, peak
        current += 1
        peak = max(peak, current)
        try:
            await asyncio.sleep(0.02)
            return await original(request)
        finally:
            current -= 1

    executor.gateway.execute = tracked
    await executor(token, jobs[0].checkpoint)
    assert peak == 2
    extractions = [item for item in provider.prompts
                   if item["candidates"][0].get("stage") != "original_image_transcription"]
    assert len(extractions) == 2
    assert executor.image_provider.await_count >= 2


@pytest.mark.asyncio
async def test_all_fields_share_each_page_call_and_validated_results():
    executor, rows, jobs, provider, token = setup_executor()
    outcome = await process(executor, rows, jobs, token)
    assert outcome.state == "done"
    assert provider.calls == 1
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
    assert outcome.state == "done" and provider.calls == 1


@pytest.mark.asyncio
async def test_invalid_page_cannot_become_not_found_or_leave_queued_siblings():
    executor, rows, jobs, provider, token = setup_executor(mode="invalid")
    outcome = await process(executor, rows, jobs, token)
    assert outcome.state == "done"
    assert outcome.semantic_state == "review_required"
    assert all(j.execution_state == "done" for j in jobs[1:])
    assert all(j.semantic_state == "review_required" for j in jobs[1:])
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
async def test_elapsed_clock_still_walks_remaining_pages():
    executor, rows, jobs, provider, token = setup_executor()
    first = await executor(token, jobs[0].checkpoint)
    await executor.write_result(rows, jobs[0], first)
    first.checkpoint["page_started_at"] = (datetime.utcnow() - timedelta(seconds=3600)).isoformat()
    outcome = await process(executor, rows, jobs, token, first.checkpoint)
    assert outcome.state == "done"
    assert provider.calls == 1
    assert outcome.checkpoint["page_progress"]["processed_pages"] == 2
    assert all(r.instances for r in rows.rows if isinstance(r, ExtractedFieldModel))
    assert all(j.execution_state == "done" for j in jobs[1:])


@pytest.mark.asyncio
async def test_readable_ocr_extracts_without_a_second_image():
    executor, rows, jobs, provider, token = setup_executor(source="ocr")
    long_text = "报告正文 瓦斯压力 1.23MPa 实测，本页文字已经足以直接抽取。" * 2
    for page in executor._revision_content.return_value["pages"]:
        page["text"] = long_text
        page["text_blocks"][0]["text"] = long_text
    outcome = await process(executor, rows, jobs, token)
    assert outcome.state == "done"
    assert provider.calls == 1
    assert outcome.checkpoint["page_progress"]["failed_units"] == 0
    assert executor.image_provider is None
    fields = [r for r in rows.rows if isinstance(r, ExtractedFieldModel)]
    assert fields and all(field.instances for field in fields)


@pytest.mark.asyncio
async def test_ocr_transcription_is_shared_across_fields_and_fact_receipts_are_bound():
    import io

    from PIL import Image

    executor, rows, jobs, provider, token = setup_executor(source="ocr", page_count=1)
    executor._revision_content.return_value["pages"][0]["text_blocks"][0]["text"] = "□" * 12 + "报告正文 瓦斯压力 1.23MPa 实测"
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
async def test_retry_exhausted_records_the_page_and_continues():
    executor, rows, jobs, provider, token = setup_executor(count=2, page_count=2)
    executor._revision_content.return_value["pages"][1]["text_blocks"][0]["source"] = "human_correction"
    executor.config = replace(executor.config, page_parallelism=1)
    first = await executor(token, jobs[0].checkpoint)
    await executor.write_result(rows, jobs[0], first)
    from app.services.model_gateway import GatewayOutcome
    executor.gateway.execute = AsyncMock(return_value=GatewayOutcome(
        "retry_exhausted", error="retry_exhausted:timeout"))
    outcome = await process(executor, rows, jobs, token, first.checkpoint)
    assert outcome.state == "done"
    assert outcome.semantic_state == "review_required"
    assert outcome.checkpoint["failed_units"]
    assert outcome.checkpoint["page_progress"]["processed_pages"] == 2
    assert all(r.instances for r in rows.rows if isinstance(r, ExtractedFieldModel))


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
    assert outcome.state == "done" and outcome.semantic_state == "review_required"
    assert provider.calls == 2
    assert outcome.checkpoint["failed_units"]


@pytest.mark.asyncio
async def test_second_split_ends_the_page_instead_of_expanding():
    executor, rows, jobs, provider, token = setup_executor(count=1, page_count=1)
    from app.services.model_gateway import GatewayOutcome
    from app.services.page_extraction import build_page_units
    executor.gateway.execute = AsyncMock(return_value=GatewayOutcome("context_overflow"))
    cp = deepcopy(jobs[0].checkpoint)
    cp["page_units"] = build_page_units(executor._revision_content.return_value["pages"])
    cp["split_depths"] = {cp["page_units"][0]["unit_id"]: 2}
    outcome = await process(executor, rows, jobs, token, cp)
    assert outcome.state == "done"
    assert outcome.semantic_state == "review_required"
    assert len(outcome.checkpoint["page_units"]) == 1
    assert outcome.checkpoint["failed_units"]
    assert executor.gateway.execute.await_count == 1


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
    assert outcome.state == "done"
    assert outcome.semantic_state == "review_required"
    assert executor.gateway.execute.await_count == 1
    assert outcome.checkpoint["failed_units"]
