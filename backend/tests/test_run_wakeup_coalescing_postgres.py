"""Run-coalesced broker delivery and fairness against the isolated G1 ledger."""

from collections import Counter
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.models.execution import ExecutionOutbox
from app.services.execution import JobRepository, QuantumOutcome
from app.services.execution.dispatch_outbox import DispatchOutbox
from app.services.reliable_extraction_service import ReliableExtractionService
from tests.test_field_scheduler_postgres import seed
from tests.test_field_scheduler_postgres import sessions as pg_sessions

sessions = pg_sessions


async def wake_fields(factory, run_id, job_ids, label):
    async with factory() as db, db.begin():
        for job_id in job_ids:
            await JobRepository(db).enqueue_outbox(kind="field_job_wakeup", aggregate_id=job_id,
                dedupe_key=f"{label}:{job_id}:{uuid4()}", payload={"run_id": str(run_id), "job_id": str(job_id)})


@pytest.mark.asyncio
async def test_many_field_events_dispatch_one_run_and_duplicate_batch_cannot_fanout(sessions):
    run_id, _, jobs = await seed(sessions, 100)
    await wake_fields(sessions, run_id, jobs, "initial")
    delivered = []

    async def send(kind, payload):
        delivered.append(payload)

    service = ReliableExtractionService()
    assert await service.dispatch_events(sessions, send, limit=100, run_id=run_id) == 1
    await wake_fields(sessions, run_id, jobs, "duplicate-new-key")
    assert await service.dispatch_events(sessions, send, limit=100, run_id=run_id) == 0
    assert len(delivered) == 1
    async with sessions() as db, db.begin():
        assert await JobRepository(db).consume_run_wakeup(run_id, UUID(delivered[0]["dispatch_event_id"]),
            dispatch_attempt=delivered[0]["dispatch_attempt"])
    assert await service.dispatch_events(sessions, send, limit=100, run_id=run_id) == 1
    assert len(delivered) == 2


@pytest.mark.asyncio
async def test_cancelled_run_wakes_are_suppressed(sessions):
    run_id, _, jobs = await seed(sessions, 10)
    await wake_fields(sessions, run_id, jobs, "cancel")
    async with sessions() as db, db.begin():
        await JobRepository(db).control_run(run_id, "cancel")
    delivered = []

    async def send(kind, payload):
        delivered.append(payload)

    assert await ReliableExtractionService().dispatch_events(sessions, send, run_id=run_id) == 0
    assert delivered == []
    async with sessions() as db:
        rows = (await db.execute(select(ExecutionOutbox).where(
            ExecutionOutbox.kind == "field_job_wakeup", ExecutionOutbox.aggregate_id.in_(jobs)))).scalars().all()
        assert all(event.dispatch_state == "suppressed" for event in rows)


@pytest.mark.asyncio
async def test_short_windows_keep_automatic_progress_without_window_fanout(sessions):
    run_id, _, jobs = await seed(sessions, 10)
    await wake_fields(sessions, run_id, jobs, "initial")
    turns = Counter()
    service = ReliableExtractionService()

    async def execute(token, checkpoint):
        turns[token.job_id] += 1
        return QuantumOutcome("yielded", {"cursor": turns[token.job_id]}) if turns[token.job_id] < 3 else (
            QuantumOutcome("done", {"cursor": 3}, semantic_state="completed"))

    deliveries = []
    async def send(kind, payload):
        deliveries.append(payload)

    status = None
    for _ in range(20):
        before = len(deliveries)
        assert await service.dispatch_events(sessions, send, run_id=run_id) <= 1
        if len(deliveries) == before:
            break
        event = deliveries[-1]
        async with sessions() as db, db.begin():
            await JobRepository(db).consume_run_wakeup(run_id, UUID(event["dispatch_event_id"]),
                dispatch_attempt=event["dispatch_attempt"], task_id="test-task")
        result = await service.process_run(sessions, run_id, executor=execute, owner="short-window", max_quantums=2)
        status = result["status"]
        if status == "COMPLETED":
            break
    assert status == "COMPLETED"
    assert sum(turns.values()) == 30
    async with sessions() as db:
        rows = (await db.execute(select(ExecutionOutbox).where(ExecutionOutbox.aggregate_id.in_(jobs)))).scalars().all()
    assert not any(":window:" in row.dedupe_key for row in rows)


@pytest.mark.asyncio
async def test_fair_runs_win_a_turn_over_a_large_old_backlog(sessions):
    older, _, old_jobs = await seed(sessions, 30)
    newer, _, new_jobs = await seed(sessions, 1)
    await wake_fields(sessions, older, old_jobs, "large")
    await wake_fields(sessions, newer, new_jobs, "small")
    async with sessions() as db, db.begin():
        events = await DispatchOutbox(db).claim_field_runs(limit=1, run_ids=[older, newer])
        assert len(events) == 1
        first = events[0]
        await DispatchOutbox(db).mark_sent(first)
        await JobRepository(db).consume_run_wakeup(older, first.event_id,
            dispatch_attempt=first.attempts, task_id="old-first")
    await wake_fields(sessions, older, old_jobs, "new-progress")
    async with sessions() as db, db.begin():
        events = await DispatchOutbox(db).claim_field_runs(limit=1, run_ids=[older, newer])
        assert len(events) == 1
        assert events[0].payload["run_id"] == str(newer)


@pytest.mark.asyncio
async def test_receipt_before_sender_ack_and_lost_receipt_are_recoverable(sessions):
    from datetime import datetime, timedelta

    run_id, _, jobs = await seed(sessions, 1)
    await wake_fields(sessions, run_id, jobs, "receipt-race")
    async with sessions() as db, db.begin():
        event = (await DispatchOutbox(db).claim_field_runs(run_id=run_id))[0]
        event_id, attempt = event.event_id, event.attempts
    # The worker can receive the broker message before the sender acknowledges.
    async with sessions() as db, db.begin():
        assert await JobRepository(db).consume_run_wakeup(run_id, event_id,
                                                         dispatch_attempt=attempt, task_id="received")
        assert not await JobRepository(db).consume_run_wakeup(run_id, event_id,
                                                              dispatch_attempt=attempt, task_id="duplicate")
    async with sessions() as db, db.begin():
        event = await db.get(ExecutionOutbox, event_id, with_for_update=True)
        await DispatchOutbox(db).mark_sent(event)
        assert event.payload["dispatch_outstanding"] is False
        assert event.next_attempt_at is None
    await wake_fields(sessions, run_id, jobs, "lost-receipt")
    async with sessions() as db, db.begin():
        event = (await DispatchOutbox(db).claim_field_runs(run_id=run_id))[0]
        await DispatchOutbox(db).mark_sent(event)
        event.next_attempt_at = datetime.utcnow() - timedelta(seconds=1)
        lost_id = event.event_id
    async with sessions() as db, db.begin():
        await DispatchOutbox(db).recover_stuck(before=datetime.utcnow()-timedelta(minutes=5),
                                              kinds=["field_job_wakeup"])
        event = await db.get(ExecutionOutbox, lost_id)
        assert event.dispatch_state == "failed"
        replacement = (await DispatchOutbox(db).claim_field_runs(run_id=run_id))[0]
        assert replacement.attempts == 2
