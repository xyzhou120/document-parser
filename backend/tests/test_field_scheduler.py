from datetime import datetime, timedelta
from uuid import uuid4

import pytest

from app.models.database import ParseRun
from app.models.execution import ParseFieldJob
from app.services.execution.field_scheduler import (
    FieldExecutionError,
    Scheduler,
    aggregate_run_status,
)


def _run(status="RUNNING"):
    return ParseRun(parse_run_id=uuid4(), document_id=uuid4(), status=status, execution_generation=3)


def _job(run, key="f"):
    return ParseFieldJob(job_id=uuid4(), run_id=run.parse_run_id, field_key=key,
                         rule_hash="a" * 64, revision_id=uuid4(), execution_state="queued",
                         semantic_state="unknown", checkpoint={}, fence_token=0,
                         progress_seq=0, attempt=0)


def test_quantum_yield_is_reclaimable_without_user_resume():
    run, job = _run(), None
    job = _job(run)
    token = Scheduler.claim(job, run, "worker-a", now=datetime(2026, 1, 1), lease_seconds=30)
    Scheduler.yield_quantum(job, run, token, {"cursor": 4}, retry_at=datetime(2026, 1, 1), now=datetime(2026, 1, 1))
    assert job.execution_state == "yielded"
    assert job.checkpoint == {"cursor": 4}
    assert job.lease_owner is None
    next_token = Scheduler.claim(job, run, "worker-b", now=datetime(2026, 1, 1), lease_seconds=30)
    assert next_token.fence_token == token.fence_token + 1


def test_expired_or_old_worker_cannot_publish():
    run, job = _run(), None
    job = _job(run)
    token = Scheduler.claim(job, run, "worker-a", now=datetime(2026, 1, 1), lease_seconds=1)
    job.lease_until = datetime(2026, 1, 1) - timedelta(seconds=1)
    with pytest.raises(FieldExecutionError):
        Scheduler.finish(job, run, token, semantic_state="completed")


def test_run_generation_change_fences_old_worker_even_with_live_lease():
    run, job = _run(), None
    job = _job(run)
    token = Scheduler.claim(job, run, "worker-a", now=datetime(2026, 1, 1), lease_seconds=30)
    run.execution_generation += 1
    with pytest.raises(FieldExecutionError):
        Scheduler.checkpoint(job, run, token, {"cursor": 9}, now=datetime(2026, 1, 1))


def test_completion_does_not_accept_partial_semantics():
    run, job = _run(), None
    job = _job(run)
    token = Scheduler.claim(job, run, "worker-a", now=datetime(2026, 1, 1), lease_seconds=30)
    with pytest.raises(FieldExecutionError):
        Scheduler.finish(job, run, token, semantic_state="partial_candidates")
    assert job.execution_state == "running"


def test_run_aggregation_control_state_has_priority():
    run = _run("PAUSED")
    jobs = [_job(run)]
    assert aggregate_run_status(run, jobs) == "PAUSED"

    run.status = "RUNNING"
    jobs[0].execution_state = "waiting_external_quota"
    assert aggregate_run_status(run, jobs) == "WAITING_EXTERNAL_QUOTA"

    jobs[0].execution_state = "done"
    jobs[0].semantic_state = "not_found"
    assert aggregate_run_status(run, jobs) == "COMPLETED"


def test_review_is_distinct_from_automatic_complete():
    run = _run()
    job = _job(run)
    job.execution_state = "done"
    job.semantic_state = "review_required"
    assert aggregate_run_status(run, [job]) == "COMPLETED_WITH_REVIEW"
