"""执行边界故障注入；锁竞争与事务原子性仍由隔离 PostgreSQL 测试验证。"""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from app.models.database import ParseRun
from app.models.execution import ParseFieldJob
from app.services.execution import FieldSchedulerRunner, QuantumOutcome, Scheduler, StaleLeaseError
from app.services.execution.field_scheduler import aggregate_run_status


@pytest.fixture
def runner_setup(monkeypatch):
    run = ParseRun(parse_run_id=uuid4(), status="RUNNING", execution_generation=0)
    job = ParseFieldJob(job_id=uuid4(), run_id=run.parse_run_id, execution_state="queued",
        checkpoint={"cursor": 7}, fence_token=0, progress_seq=0, attempt=0)
    db = SimpleNamespace(get=AsyncMock(return_value=job))

    @asynccontextmanager
    async def context():
        yield db

    db.begin = context

    class Repository:
        published = []
        beats = 0

        def __init__(self, _db):
            pass

        async def claim_next(self, **kwargs):
            return Scheduler.claim(job, run, kwargs["owner"], lease_seconds=kwargs["lease_seconds"])

        async def transition(self, token, action, **kwargs):
            assert action == "heartbeat"
            Scheduler.heartbeat(job, run, token, **kwargs)
            self.__class__.beats += 1

        async def publish(self, token, outcome, *, writer=None, lease_seconds=90):
            self.__class__.published.append(outcome)
            if writer:
                await writer(db, job, outcome)
            if outcome.state == "waiting_service":
                Scheduler.wait(job, run, token, outcome.state, retry_at=outcome.retry_at, error=outcome.error)
            elif outcome.state == "failed_terminal":
                Scheduler.checkpoint(job, run, token, outcome.checkpoint)
                Scheduler.fail_terminal(job, run, token, outcome.error)
            else:
                Scheduler.finish(job, run, token, semantic_state=outcome.semantic_state, checkpoint=outcome.checkpoint)

        async def aggregate(self, _run_id):
            run.status = aggregate_run_status(run, [job])

    monkeypatch.setattr("app.services.execution.job_repository.JobRepository", Repository)
    return FieldSchedulerRunner(context), run, job, Repository


async def test_long_quantum_renews_lease_and_publishes(runner_setup):
    runner, run, job, repository = runner_setup
    async def execute(token, cp):
        await asyncio.sleep(2.1)
        return QuantumOutcome("done", cp, semantic_state="completed")
    await runner.run_next(run_id=run.parse_run_id, owner="test", execute=execute, lease_seconds=2)
    assert repository.beats >= 2
    assert job.execution_state == "done" and run.status == "COMPLETED"


async def test_cancelled_lease_stops_inflight_work_without_publishing(runner_setup):
    runner, run, job, repository = runner_setup
    cancelled = asyncio.Event()
    async def execute(token, cp):
        run.status = "CANCELLED"
        run.execution_generation += 1
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    with pytest.raises(StaleLeaseError):
        await runner.run_next(run_id=run.parse_run_id, owner="test", execute=execute, lease_seconds=2)
    assert cancelled.is_set() and repository.published == []
    assert run.status == "CANCELLED"


async def test_redis_outage_becomes_durable_wait_and_does_not_run_domain_writer(runner_setup):
    runner, run, job, repository = runner_setup
    async def execute(token, cp):
        raise RedisConnectionError("unavailable")
    writer = AsyncMock()
    await runner.run_next(run_id=run.parse_run_id, owner="test", execute=execute, write_result=writer)
    assert job.execution_state == "waiting_service" and run.status == "WAITING_SERVICE"
    assert job.next_attempt_at > datetime.utcnow()
    assert repository.published[0].checkpoint["cursor"] == 7
    assert repository.published[0].checkpoint["infrastructure_failures"] == 1
    writer.assert_not_awaited()


async def test_repeated_infrastructure_failure_is_bounded(runner_setup):
    runner, run, job, repository = runner_setup
    job.checkpoint["infrastructure_failures"] = 6
    async def execute(token, cp):
        raise RedisConnectionError("unavailable")
    await runner.run_next(run_id=run.parse_run_id, owner="test", execute=execute)
    assert job.execution_state == "failed_terminal" and run.status == "FAILED"
    assert job.checkpoint["cursor"] == 7


async def test_success_resets_infrastructure_failure_budget(runner_setup):
    runner, run, job, repository = runner_setup
    job.checkpoint["infrastructure_failures"] = 6
    async def execute(token, cp):
        return QuantumOutcome("done", deepcopy(cp), semantic_state="completed")
    await runner.run_next(run_id=run.parse_run_id, owner="test", execute=execute)
    assert "infrastructure_failures" not in job.checkpoint


async def test_unexpected_input_error_is_terminal_instead_of_running_forever(runner_setup):
    runner, run, job, repository = runner_setup
    async def execute(token, cp):
        raise ValueError("bad source")
    await runner.run_next(run_id=run.parse_run_id, owner="test", execute=execute)
    assert job.execution_state == "failed_terminal" and run.status == "FAILED"


async def test_domain_writer_failure_also_converges_to_terminal(runner_setup):
    runner, run, job, repository = runner_setup
    async def execute(token, cp):
        return QuantumOutcome("done", cp, semantic_state="completed")
    writer = AsyncMock(side_effect=ValueError("invalid field payload"))
    await runner.run_next(run_id=run.parse_run_id, owner="test", execute=execute, write_result=writer)
    assert job.execution_state == "failed_terminal" and run.status == "FAILED"
    assert job.checkpoint["cursor"] == 7
    writer.assert_awaited_once()


async def test_failed_page_coordinator_converges_all_unfinished_siblings():
    from app.services.execution import JobRepository
    run = ParseRun(parse_run_id=uuid4(), status="RUNNING", execution_generation=0,
        extraction_profile={"extraction_strategy": "page_batch_v1", "coordinator_field_key": "a"})
    jobs = [ParseFieldJob(job_id=uuid4(), run_id=run.parse_run_id, field_key=key,
        execution_state="queued", semantic_state="unknown", checkpoint={}, fence_token=0, progress_seq=0, attempt=0)
        for key in ("a", "b", "c")]
    jobs[2].execution_state, jobs[2].semantic_state = "done", "completed"
    token = Scheduler.claim(jobs[0], run, "worker")
    db = SimpleNamespace(scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: jobs)))
    repo = JobRepository(db)
    repo._locked = AsyncMock(return_value=(jobs[0], run))
    await repo.publish(token, QuantumOutcome("failed_terminal", {"cursor": 7}, error="bad input"))
    assert [job.execution_state for job in jobs] == ["failed_terminal", "failed_terminal", "done"]
    assert jobs[2].semantic_state == "completed"
    assert aggregate_run_status(run, jobs) == "FAILED"


async def test_content_worker_keeps_lease_while_ocr_runs():
    from app.models.content import ContentBuildJob
    from app.services.content_build_service import ContentBuildService
    from app.services.content_preparation_service import ContentPreparationService
    job = ContentBuildJob(build_id=uuid4(), fence_token=0)
    db = SimpleNamespace(get=AsyncMock(return_value=job))
    @asynccontextmanager
    async def context():
        yield db
    db.begin = context
    builder = ContentBuildService(lease_seconds=2)
    async def process(*args, worker_id, **kwargs):
        job.fence_token = 1
        job.lease_owner = worker_id
        job.lease_until = datetime.utcnow() + timedelta(seconds=2)
        await asyncio.sleep(2.1)
        assert job.lease_until > datetime.utcnow()
        return {"ready": True}
    builder.process = process
    service = ContentPreparationService(builder=builder)
    assert await service._process_with_heartbeat(context, db, job, [], worker_id="test") == {"ready": True}


async def test_content_recovery_never_revokes_live_worker_and_bounds_recovery():
    from app.models.content import ContentBuildJob
    from app.services.content_preparation_service import ContentPreparationService
    live = ContentBuildJob(build_id=uuid4(), lease_owner="live", fence_token=2,
        lease_until=datetime.utcnow() + timedelta(seconds=60), checkpoint={})
    exhausted = ContentBuildJob(build_id=uuid4(), fence_token=1, checkpoint={"worker_recoveries": 6})
    db = SimpleNamespace(scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: [live, exhausted])),
                         scalar=AsyncMock(return_value=None))
    assert await ContentPreparationService().recover_builds(db) == 0
    assert live.lease_owner == "live" and live.fence_token == 2
    assert exhausted.dispatch_state == "failed_terminal"


def test_page_siblings_and_future_backoff_are_not_runnable():
    from app.services.reliable_extraction_service import runnable_jobs
    now = datetime.utcnow()
    run = ParseRun(extraction_profile={"extraction_strategy": "page_batch_v1", "coordinator_field_key": "a"})
    coordinator = ParseFieldJob(field_key="a", execution_state="waiting_service")
    sibling = ParseFieldJob(field_key="b", execution_state="queued")
    assert runnable_jobs(run, [coordinator, sibling], now=now) == []
    coordinator.execution_state = "yielded"
    coordinator.next_attempt_at = now + timedelta(seconds=10)
    assert runnable_jobs(run, [coordinator, sibling], now=now) == []
    coordinator.next_attempt_at = now
    assert runnable_jobs(run, [coordinator, sibling], now=now) == [coordinator]


def test_celery_retry_does_not_reuse_consumed_dispatch_receipt(monkeypatch):
    from celery.exceptions import Retry
    from unittest.mock import Mock
    from app.tasks import reliable_execution_wakeup_task
    def unavailable(coroutine):
        coroutine.close()
        raise RedisConnectionError("broker unavailable")
    retry = Mock(side_effect=Retry())
    monkeypatch.setattr("asyncio.run", unavailable)
    monkeypatch.setattr(reliable_execution_wakeup_task, "retry", retry)
    monkeypatch.setattr("app.tasks.settings.app.reliable_enabled", True)
    run_id = str(uuid4())
    reliable_execution_wakeup_task.push_request(retries=0)
    try:
        with pytest.raises(Retry):
            reliable_execution_wakeup_task.run(run_id, str(uuid4()), 1)
    finally:
        reliable_execution_wakeup_task.pop_request()
    assert retry.call_args.kwargs["args"] == [run_id]
    assert retry.call_args.kwargs["kwargs"] == {}
    assert retry.call_args.kwargs["countdown"] == 15


def test_stale_worker_exits_without_marking_new_generation_failed(monkeypatch):
    from app.tasks import reliable_execution_wakeup_task
    calls = 0
    def stale(coroutine):
        nonlocal calls
        calls += 1
        coroutine.close()
        raise StaleLeaseError("stale or foreign field lease")
    monkeypatch.setattr("asyncio.run", stale)
    monkeypatch.setattr("app.tasks.settings.app.reliable_enabled", True)
    result = reliable_execution_wakeup_task.run(str(uuid4()))
    assert result["status"] == "lease_lost"
    assert calls == 1  # 没有进入另一个 asyncio.run 来结束接管者的任务。
