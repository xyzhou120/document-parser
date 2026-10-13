"""Bounded extraction progress and SQL work, with explicitly selected G1 PostgreSQL."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import event, select

from app.models.database import Document, DocumentArtifact
from app.models.execution import ParseCandidate, ParseFieldJob
from app.rules.spec import RuleSpec
from app.services.call_ledger import CallLedger
from app.services.candidate_repository import CandidateRepository
from app.services.content_service import ContentService
from app.services.model_gateway import GatewayPolicy, ModelGateway
from app.services.reliable_extraction_service import ReliableExtractionService
from app.services.reliable_field_executor import (
    _REVISION_CACHE,
    _REVISION_CACHE_LIMIT,
    ReliableFieldExecutor,
    ReliableFieldExecutorConfig,
    compact_checkpoint_payload,
    model_candidate_payload,
)
from app.services.retrieval_plan import build_range_shards
from app.services.service_circuit import MemoryCircuitStore, ServiceCircuit
from tests.test_field_scheduler_postgres import seed
from tests.test_field_scheduler_postgres import sessions as pg_sessions
from tests.test_reliable_field_executor import make_run

sessions = pg_sessions


class ControlledProvider:
    def __init__(self, mode="rejected"):
        self.mode, self.calls, self.prompts = mode, 0, []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self, **kwargs):
        self.calls += 1
        payload = json.loads(kwargs["messages"][-1]["content"][0]["text"])
        self.prompts.append(payload)
        content = "invalid JSON" if self.mode == "invalid" else json.dumps({"decisions": [
            {"candidate_id": c["candidate_id"], "judgement": "rejected"} for c in payload["candidates"]]})
        return {"choices": [{"finish_reason": "length" if self.mode == "length" else "stop",
                             "message": {"content": content}}],
                "usage": {"prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70}}


class CountingContent(ContentService):
    def __init__(self):
        super().__init__()
        self.reads = 0

    async def read_revision(self, db, revision_id):
        self.reads += 1
        return await super().read_revision(db, revision_id)


def test_model_payload_does_not_reinsert_historical_evidence_or_base64():
    image = "data:image/png;base64," + "A" * 10000
    payload = model_candidate_payload({"candidate_id": "c", "text": "原文", "source": "ocr",
        "evidence": {"registry": {"old": {"image_ref": image}}}, "image_refs": [image],
        "evidence_ids": ["image:c"], "rule": {"field_key": "gas"}})
    assert "evidence" not in payload and "image_refs" not in payload
    assert image not in json.dumps(payload)
    compact = compact_checkpoint_payload({"candidates": [{"payload": payload, "image_refs": [image]}], "image_ref": image})
    assert "data:image/" not in json.dumps(compact)


@pytest.mark.asyncio
async def test_fixed_revision_cache_is_bounded_and_reused_across_executor_windows():
    content = SimpleNamespace(read_revision=AsyncMock(return_value=SimpleNamespace(
        content=SimpleNamespace(pages=[{"page_number": 1, "text": "原文"}], coverage_complete=True))))
    executor = ReliableFieldExecutor(None, None, ReliableFieldExecutorConfig("test", "test"), content=content)
    revisions = [uuid4() for _ in range(_REVISION_CACHE_LIMIT + 1)]
    for revision in revisions:
        await executor._revision_content(None, revision)
    assert len(_REVISION_CACHE) <= _REVISION_CACHE_LIMIT
    reads = content.read_revision.await_count
    second_window = ReliableFieldExecutor(None, None, executor.config, content=content)
    await second_window._revision_content(None, revisions[-1])
    assert content.read_revision.await_count == reads
    await executor._revision_content(None, revisions[0])
    assert content.read_revision.await_count == reads + 1


@pytest.mark.asyncio
async def test_existing_116_range_items_require_one_select_and_zero_writes(sessions):
    run_id, _, job_ids = await seed(sessions, 1)
    shards = build_range_shards([{"page_number": number} for number in range(1, 30)], pages_per_range=1)
    assert len(shards) == 116
    engine = sessions.kw["bind"].sync_engine
    statements = []
    def record(conn, cursor, statement, parameters, context, executemany):
        if "parse_range_work_items" in statement:
            statements.append(statement.strip().split()[0].upper())
    event.listen(engine, "before_cursor_execute", record)
    try:
        async with sessions() as db, db.begin():
            ranges = await CandidateRepository(db).initialize_ranges(job_ids[0], shards)
            assert len(ranges) == 116
        statements.clear()
        async with sessions() as db, db.begin():
            ranges = await CandidateRepository(db).initialize_ranges(job_ids[0], shards)
            assert len(ranges) == 116
        assert statements == ["SELECT"]
    finally:
        event.remove(engine, "before_cursor_execute", record)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["invalid", "length", "context"])
async def test_minimum_invalid_candidate_terminates_and_is_not_replayed_by_fallback(sessions, mode):
    run_id = await make_run(sessions)
    provider = ControlledProvider(mode)
    policy = GatewayPolicy(context_capacity=2800, min_output_tokens=2500, max_output_tokens=2500) if mode == "context" else GatewayPolicy()
    executor = ReliableFieldExecutor(sessions,
        ModelGateway(provider, CallLedger(sessions), ServiceCircuit(MemoryCircuitStore()), policy),
        ReliableFieldExecutorConfig("test", "test", semantic_retry_max=2))
    service = ReliableExtractionService()
    result = await service.process_run(sessions, run_id, executor=executor, writer=executor.write_result,
        before_publish=executor.before_publish, owner="bounded-failure", max_quantums=20)
    assert result["status"] == "FAILED"
    assert result["quantums_executed"] < 15
    assert provider.calls == (0 if mode == "context" else 1)
    async with sessions() as db:
        job = await db.scalar(select(ParseFieldJob).where(ParseFieldJob.run_id == run_id))
        rows = (await db.execute(select(ParseCandidate).where(ParseCandidate.job_id == job.job_id))).scalars().all()
        assert rows and all(row.state == "unresolved" for row in rows)
        assert job.execution_state == "failed_terminal" and job.semantic_state == "incomplete"
        assert job.checkpoint["failed_window_keys"]
        assert any((row.evidence or {}).get("registry") for row in rows)


@pytest.mark.asyncio
async def test_failed_batch_id_is_not_recreated_or_sent_again():
    gateway = SimpleNamespace(execute=AsyncMock())
    executor = ReliableFieldExecutor(None, gateway, ReliableFieldExecutorConfig("test", "test"))
    cid = str(uuid4())
    checkpoint = {"candidate_batches": {"failed": {"batch_state": "failed", "candidate_ids": [cid]}}}
    outcome = await executor._judge(None, checkpoint, [{"candidate_id": cid}], None, None, [], {})
    assert outcome.state == "yielded" and outcome.checkpoint["unresolved_ids"] == [cid]
    assert outcome.checkpoint["proof_error"] == "failed_candidate_replay_blocked"
    gateway.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_29_pages_12_rules_finish_without_reinserting_the_range_plan(sessions):
    document_id = uuid4()
    pages = [{"page_number": n, "text": "报告正文 本页为普通说明", "native_text": "报告正文 本页为普通说明",
        "width": 600, "height": 800, "image_count": 0, "ocr_status": "not_needed", "tables": [],
        "text_blocks": [{"block_id": f"p{n}-body", "source": "native", "text": "报告正文 本页为普通说明", "bbox": None}]}
        for n in range(1, 30)]
    rules = [RuleSpec(field_key=f"pressure{i}", field_name_cn="瓦斯压力", data_type="float", unit="MPa") for i in range(12)]
    service = ReliableExtractionService()
    async with sessions() as db, db.begin():
        db.add(Document(document_id=document_id, file_name="bounded-29.pdf", original_name="synthetic-bounded-29.pdf",
            file_path="test/bounded29", file_hash="a" * 64, file_size=1, mime_type="application/pdf"))
        await db.flush()
        db.add(DocumentArtifact(document_id=document_id, file_hash="a" * 64, status="READY",
            preprocess_profile_version="progress-test", pages=pages, quality={"coverage_manifest": {
                "expected_page_count": 29, "pages": [{"page_number": n, "scope_complete": True,
                    "regions": [{"state": "processed"}]} for n in range(1, 30)]}}))
        await db.flush()
        run = await service.start(db, document_id, rules=rules)
        run_id = run.parse_run_id
    provider, content = ControlledProvider(), CountingContent()
    executor = ReliableFieldExecutor(sessions,
        ModelGateway(provider, CallLedger(sessions), ServiceCircuit(MemoryCircuitStore())),
        ReliableFieldExecutorConfig("test", "test"), content=content)
    engine = sessions.kw["bind"].sync_engine
    inserts = []
    def record(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("INSERT INTO PARSE_RANGE_WORK_ITEMS"):
            inserts.append(statement)
    event.listen(engine, "before_cursor_execute", record)
    try:
        result = await service.process_run(sessions, run_id, executor=executor, writer=executor.write_result,
            before_publish=executor.before_publish, owner="bounded-29x12", max_quantums=800, lease_seconds=300)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert result["status"] == "COMPLETED"
    assert result["quantums_executed"] <= 12 * 50
    assert len(inserts) == 12
    assert content.reads == 1
    async with sessions() as db:
        jobs = (await db.execute(select(ParseFieldJob).where(ParseFieldJob.run_id == run_id))).scalars().all()
        assert len(jobs) == 12 and all(job.semantic_state == "not_found" for job in jobs)
