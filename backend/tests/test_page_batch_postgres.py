"""Page batching through real PostgreSQL leases, gateway ledger and fact writer."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.models.database import ExtractedFieldModel, ParseRun
from app.models.execution import ParseFieldJob
from app.services.call_ledger import CallLedger
from app.services.execution import JobRepository
from app.services.model_gateway import ModelGateway
from app.services.reliable_extraction_service import ReliableExtractionService
from app.services.reliable_field_executor import ReliableFieldExecutorConfig
from app.services.reliable_page_executor import ReliablePageExecutor
from app.services.service_circuit import MemoryCircuitStore, ServiceCircuit
from tests.test_field_scheduler_postgres import sessions as pg_sessions
from tests.test_reliable_field_executor import make_run

sessions = pg_sessions


class SharedProvider:
    def __init__(self):
        self.calls = 0
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **kwargs):
        self.calls += 1
        candidate = json.loads(kwargs["messages"][-1]["content"][0]["text"])["candidates"][0]
        facts = [{"field_key": rule["field_key"], "value": "1.23", "quote": "报告正文 瓦斯压力 1.23MPa 实测",
            "unit": "MPa", "value_kind": "measured", "source_role": "report_body", "dimensions": {},
            "evidence_ids": candidate["evidence_ids"]} for rule in candidate["rules"]]
        return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({
            "decisions": [{"candidate_id": candidate["candidate_id"], "judgement": "accepted", "facts": facts}]})}}],
            "usage": {"prompt_tokens": 40, "completion_tokens": 20, "total_tokens": 60}}


def executor(sessions, provider):
    return ReliablePageExecutor(sessions,
        ModelGateway(provider, CallLedger(sessions), ServiceCircuit(MemoryCircuitStore())),
        ReliableFieldExecutorConfig("page-pg-test", "test"))


@pytest.mark.asyncio
async def test_real_postgres_all_fields_share_call_and_finish(sessions):
    run_id = await make_run(sessions, 12, extraction_strategy="page_batch_v1")
    provider = SharedProvider()
    instance = executor(sessions, provider)
    result = await ReliableExtractionService().process_run(sessions, run_id, executor=instance,
        writer=instance.write_result, before_publish=instance.before_publish, owner="page-pg",
        max_quantums=5, lease_seconds=300)
    assert result["status"] == "COMPLETED"
    assert provider.calls == 1
    async with sessions() as db:
        fields = list((await db.execute(select(ExtractedFieldModel).where(
            ExtractedFieldModel.parse_run_id == run_id))).scalars().all())
        assert len(fields) == 12 and all(field.instances for field in fields)
        run = await db.get(ParseRun, run_id)
        assert run.started_at is not None and run.completed_at is not None
        assert run.duration_seconds >= 0


@pytest.mark.asyncio
async def test_only_one_worker_can_claim_page_coordinator(sessions):
    run_id = await make_run(sessions, 12, extraction_strategy="page_batch_v1")

    async def claim(owner):
        async with sessions() as db, db.begin():
            return await JobRepository(db).claim_next(run_id=run_id, owner=owner, lease_seconds=300)

    tokens = await asyncio.gather(claim("one"), claim("two"))
    assert sum(token is not None for token in tokens) == 1
    token = next(token for token in tokens if token is not None)
    async with sessions() as db:
        job = await db.get(ParseFieldJob, token.job_id)
        run = await db.get(ParseRun, run_id)
        assert job.field_key == run.extraction_profile["coordinator_field_key"]


@pytest.mark.asyncio
async def test_saved_gateway_result_replays_after_expired_worker_without_provider_repeat(sessions):
    run_id = await make_run(sessions, 2, extraction_strategy="page_batch_v1")
    provider = SharedProvider()
    instance = executor(sessions, provider)
    async with sessions() as db, db.begin():
        token = await JobRepository(db).claim_next(run_id=run_id, owner="crashed", lease_seconds=300)
        job = await db.get(ParseFieldJob, token.job_id)
        checkpoint = dict(job.checkpoint)
    # Provider completion is durable; simulate process death before checkpoint publication.
    await instance(token, checkpoint)
    assert provider.calls == 1
    from datetime import datetime, timedelta
    async with sessions() as db, db.begin():
        job = await db.get(ParseFieldJob, token.job_id, with_for_update=True)
        job.lease_until = datetime.utcnow() - timedelta(seconds=1)
        await JobRepository(db).requeue_due()
    resumed = executor(sessions, provider)
    result = await ReliableExtractionService().process_run(sessions, run_id, executor=resumed,
        writer=resumed.write_result, before_publish=resumed.before_publish, owner="recovered",
        max_quantums=5, lease_seconds=300)
    assert result["status"] == "COMPLETED"
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_v2_four_native_pages_survive_loss_of_entire_checkpoint(sessions, monkeypatch):
    from app.config import settings
    from app.models.execution import ParseModelCall
    from sqlalchemy import func
    from dataclasses import replace
    monkeypatch.setattr(settings.agent, 'reliable_ledger_prefetch_enabled', True)
    monkeypatch.setattr(settings.agent, 'reliable_page_parallelism', 4)
    run_id = await make_run(sessions, 1, page_count=4, extraction_strategy='page_batch_v1',
        text_override='报告正文 瓦斯压力 1.23MPa 实测。' * 140)
    provider = SharedProvider()
    instance = executor(sessions, provider)
    instance.config = replace(instance.config, page_parallelism=4)
    async with sessions() as db, db.begin():
        token = await JobRepository(db).claim_next(run_id=run_id, owner='crashed-partners', lease_seconds=300)
        job = await db.get(ParseFieldJob, token.job_id)
        checkpoint = dict(job.checkpoint)
    lost = await instance(token, checkpoint)
    assert provider.calls == 4
    assert len(lost.checkpoint['prefetched_calls']) == 3
    # 故障注入：模型账本已保存，writer 与 checkpoint 尚未提交。
    from datetime import datetime, timedelta
    async with sessions() as db, db.begin():
        job = await db.get(ParseFieldJob, token.job_id, with_for_update=True)
        job.lease_until = datetime.utcnow() - timedelta(seconds=1)
        await JobRepository(db).requeue_due()
    resumed = executor(sessions, provider)
    resumed.config = replace(resumed.config, page_parallelism=4)
    result = await ReliableExtractionService().process_run(sessions, run_id, executor=resumed,
        writer=resumed.write_result, before_publish=resumed.before_publish,
        owner='recovered-partners', max_quantums=10, lease_seconds=300)
    assert result['status'] == 'COMPLETED'
    assert provider.calls == 4
    async with sessions() as db:
        assert await db.scalar(select(func.count()).select_from(ParseModelCall).where(
            ParseModelCall.job_id == token.job_id)) == 4
        job = await db.get(ParseFieldJob, token.job_id)
        assert not job.checkpoint['prefetched_calls']


@pytest.mark.asyncio
async def test_global_quantum_slots_cap_actual_workers_and_release_on_yield(sessions, monkeypatch):
    from app.config import settings
    from app.services.execution import QuantumOutcome
    monkeypatch.setattr(settings.agent, 'reliable_quantum_queue_enabled', True)
    monkeypatch.setattr(settings.attachment_import, 'parse_concurrency', 1)
    # 独立库可能保留之前故障注入的活动租约，只统计本轮通过唯一 owner 领取的记录。
    from sqlalchemy import update
    from datetime import datetime, timedelta
    async with sessions() as db, db.begin():
        await db.execute(update(ParseFieldJob).where(ParseFieldJob.execution_state == 'running')
            .values(lease_until=datetime.utcnow() - timedelta(seconds=1)))
        await JobRepository(db).requeue_due(limit=1000)
    one, two = [await make_run(sessions, 1, extraction_strategy='page_batch_v1') for _ in range(2)]
    async def claim(identity):
        async with sessions() as db, db.begin():
            return await JobRepository(db).claim_next(run_id=identity, owner='global-quantum', lease_seconds=300)
    tokens = await asyncio.gather(claim(one), claim(two))
    assert sum(t is not None for t in tokens) == 1
    held = next(t for t in tokens if t)
    other = two if held.run_id == one else one
    async with sessions() as db, db.begin():
        await JobRepository(db).transition(held, 'yield', checkpoint={'cursor': 1})
    assert await claim(other) is not None
