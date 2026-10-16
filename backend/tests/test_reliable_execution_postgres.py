"""P13-A 显式隔离 PostgreSQL 的恢复、控制和发件箱集成测试。"""

from collections import Counter
from datetime import datetime, timedelta

import pytest

from app.services.execution import JobRepository, QuantumOutcome
from app.services.execution import DispatchOutbox
from app.models.execution import ExecutionOutbox, ParseFieldJob
from sqlalchemy import select, func
from app.models.database import ParseRun
from app.services.reliable_extraction_service import ReliableExtractionService
from tests.test_field_scheduler_postgres import seed
from tests.test_field_scheduler_postgres import sessions as pg_sessions

sessions = pg_sessions


@pytest.mark.asyncio
async def test_repeated_consumed_empty_claim_rearms_one_durable_wakeup(sessions, monkeypatch):
    """真实 run 行竞争 + 稳定进度：每次消费后都仍可续投，不依赖 idle 超时。"""
    run_id, _, ids = await seed(sessions, 1)
    service = ReliableExtractionService()

    async def empty_claim(self, **kwargs):
        async with sessions() as blocker, blocker.begin():
            await blocker.get(ParseRun, run_id, with_for_update=True)
            async with sessions() as claimant, claimant.begin():
                assert await JobRepository(claimant).claim_next(run_id=run_id, owner="contended") is None
        return None

    monkeypatch.setattr("app.services.execution.field_scheduler.FieldSchedulerRunner.run_next", empty_claim)
    async def unused(*args):
        raise AssertionError("empty claim must not invoke model executor")
    initial = await service.process_run(sessions, run_id, executor=unused, owner="empty", max_quantums=1)
    assert initial["quantums_executed"] == 0 and initial["remaining_runnable"] == 1
    event_id = None
    for attempt in range(1, 4):
        async with sessions() as db, db.begin():
            events = await DispatchOutbox(db).claim_field_runs(run_id=run_id)
            assert len(events) == 1
            event = events[0]
            event_id = event_id or event.event_id
            assert event.event_id == event_id and event.attempts == attempt
            # 故意在 sender mark_sent 之前消费，覆盖快速 worker 竞争窗口。
        async with sessions() as db, db.begin():
            if attempt > 1:
                assert not await JobRepository(db).consume_run_wakeup(run_id, event_id, dispatch_attempt=attempt - 1)
            assert await JobRepository(db).consume_run_wakeup(run_id, event_id, dispatch_attempt=attempt)
        await service.process_run(sessions, run_id, executor=unused, owner="empty", max_quantums=1)
        async with sessions() as db:
            event = await db.get(ExecutionOutbox, event_id)
            assert event.dispatch_state == "pending" and not event.payload["dispatch_outstanding"]
            assert await db.scalar(select(func.count()).select_from(ExecutionOutbox).where(
                ExecutionOutbox.aggregate_id.in_(ids))) == 1
        async with sessions() as db, db.begin():
            assert not await JobRepository(db).consume_run_wakeup(run_id, event_id, dispatch_attempt=attempt)
    monkeypatch.undo()
    async def finish(token, checkpoint):
        return QuantumOutcome("done", checkpoint, semantic_state="completed")
    result = await service.process_run(sessions, run_id, executor=finish, owner="recovered", max_quantums=1)
    assert result["quantums_executed"] == 1 and result["status"] == "COMPLETED"


@pytest.mark.asyncio
async def test_dispatch_preserves_pending_wakeup_during_live_lease(sessions):
    run_id, _, ids = await seed(sessions, 1)
    async with sessions() as db, db.begin():
        repo = JobRepository(db)
        token = await repo.claim_next(run_id=run_id, owner="live")
        await repo.enqueue_outbox(kind="field_job_wakeup", aggregate_id=ids[0],
            dedupe_key=f"lease-test:{run_id}", payload={"run_id": str(run_id)})
    async with sessions() as db, db.begin():
        assert await DispatchOutbox(db).claim_field_runs(run_id=run_id) == []
        assert (await db.scalar(select(ExecutionOutbox).where(
            ExecutionOutbox.aggregate_id == ids[0]))).dispatch_state == "pending"
        await JobRepository(db).transition(token, "yield", checkpoint={})
    async with sessions() as db, db.begin():
        assert len(await DispatchOutbox(db).claim_field_runs(run_id=run_id)) == 1


@pytest.mark.asyncio
async def test_waiting_page_coordinator_does_not_emit_sibling_wakeup_storm(sessions):
    run_id, revision_id, ids = await seed(sessions, 3)
    async with sessions() as db, db.begin():
        run = await db.get(ParseRun, run_id)
        run.extraction_profile = {"extraction_strategy": "page_batch_v1", "coordinator_field_key": "f000"}
    async def execute(token, checkpoint):
        return QuantumOutcome("waiting_service", checkpoint,
                              retry_at=datetime.utcnow() + timedelta(seconds=60), error="outage")
    result = await ReliableExtractionService().process_run(sessions, run_id, executor=execute,
        owner="outage", max_quantums=5)
    assert result["quantums_executed"] == 1
    assert result["remaining_runnable"] == 0
    assert result["status"] == "WAITING_SERVICE"


@pytest.mark.asyncio
async def test_injected_quantums_auto_continue_and_status_is_database_backed(sessions):
    run_id, _, _ = await seed(sessions, 10)
    called = Counter()

    async def execute(token, checkpoint):
        called[token.job_id] += 1
        if called[token.job_id] < 4:
            return QuantumOutcome("yielded", {"cursor": called[token.job_id]})
        return QuantumOutcome("done", {"cursor": 4}, semantic_state="completed")

    service = ReliableExtractionService()
    first = await service.process_run(sessions, run_id, executor=execute, owner="integration",
                                      max_quantums=11)
    assert first["status"] != "COMPLETED"
    assert first["remaining_runnable"] > 0
    second = await service.process_run(sessions, run_id, executor=execute, owner="integration",
                                       max_quantums=40)
    assert second["status"] == "COMPLETED"
    assert sum(called.values()) == 40
    async with sessions() as db:
        status = await service.execution(db, run_id, limit=3)
    assert status["total_fields"] == 10
    assert status["automatic_complete"] is True
    assert len(status["jobs"]) == 3
    assert status["next_cursor"] is not None


@pytest.mark.asyncio
async def test_control_is_ledger_backed_and_resumption_dispatches_after_commit(sessions):
    run_id, _, _ = await seed(sessions, 2)
    service = ReliableExtractionService()
    async with sessions() as db, db.begin():
        paused = await service.control(db, run_id, "pause")
        assert paused["status"] == "PAUSED"
    async with sessions() as db, db.begin():
        resumed = await service.control(db, run_id, "resume")
        assert resumed["execution_generation"] == paused["execution_generation"] + 1
    delivered = []

    async def send(kind, payload):
        delivered.append((kind, payload))

    assert await service.dispatch_events(sessions, send, limit=100, run_id=run_id) > 0
    assert any(payload.get("run_id") == str(run_id) for _, payload in delivered)


@pytest.mark.asyncio
async def test_model_quantum_can_pause_run_from_another_session_without_db_lock(sessions):
    run_id, _, _ = await seed(sessions, 1)

    async def execute(token, checkpoint):
        async with sessions() as db, db.begin():
            await JobRepository(db).control_run(run_id, "pause")
        return QuantumOutcome("done", {}, semantic_state="completed")

    from app.services.execution import StaleLeaseError
    with pytest.raises(StaleLeaseError):
        await ReliableExtractionService().process_run(sessions, run_id, executor=execute,
                                                      owner="late-return", max_quantums=1)


@pytest.mark.asyncio
async def test_explicit_external_quota_resume_requeues_and_preserves_call_lineage(sessions):
    run_id, _, _ = await seed(sessions, 1)
    async with sessions() as db, db.begin():
        repository = JobRepository(db)
        token = await repository.claim_next(run_id=run_id, owner="a")
        await repository.transition(token, "checkpoint", checkpoint={"prior_call_id": "prior", "cursor": 4})
        await repository.transition(token, "wait", state="waiting_external_quota", retry_at=None)
        await repository.aggregate(run_id)
    service = ReliableExtractionService()
    async with sessions() as db, db.begin():
        result = await service.control(db, run_id, "resume")
        assert result["status"] == "RUNNING"
        replacement = await JobRepository(db).claim_next(run_id=run_id, owner="b")
        assert replacement.job_id == token.job_id
        assert replacement.execution_generation > token.execution_generation
    async with sessions() as db:
        payload = await service.execution(db, run_id)
        assert payload["jobs"][0].checkpoint == {"prior_call_id": "prior", "cursor": 4}
