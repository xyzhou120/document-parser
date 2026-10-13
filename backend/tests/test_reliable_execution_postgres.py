"""P13-A 显式隔离 PostgreSQL 的恢复、控制和发件箱集成测试。"""

from collections import Counter

import pytest

from app.services.execution import JobRepository, QuantumOutcome
from app.services.reliable_extraction_service import ReliableExtractionService
from tests.test_field_scheduler_postgres import seed
from tests.test_field_scheduler_postgres import sessions as pg_sessions

sessions = pg_sessions


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
