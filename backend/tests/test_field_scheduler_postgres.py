"""针对显式选择的隔离 PostgreSQL 数据库执行 P05 并发测试。

锁、栅栏或发件箱领取不使用 SQLite 或模拟会话。测试追加名称唯一的模拟记录，
并保留不可变内容审计。
"""

import asyncio
import os
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.models.content import ContentRevision
from app.models.database import Document, DocumentArtifact, ParseRun
from app.models.execution import ExecutionOutbox, ParseFieldJob
from app.models.source_revisions import SourceRevision  # noqa: F401 - 注册外键元数据
from app.services.execution import DispatchOutbox, JobRepository, StaleLeaseError


@pytest_asyncio.fixture
async def sessions():
    name = os.environ.get("P05_TEST_DATABASE_NAME", "")
    if not name:
        pytest.skip("set P05_TEST_DATABASE_NAME to the isolated G1 database")
    if not name.startswith("document_parser_v2_g1_"):
        raise ValueError("P05 database name must use the isolated G1 prefix")
    url = make_url(settings.database.url).set(database=name)
    engine = create_async_engine(url, echo=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def seed(sessions, count=3):
    document_id, artifact_id, revision_id, run_id = (uuid4() for _ in range(4))
    async with sessions() as db, db.begin():
        db.add(Document(document_id=document_id, file_name=f"p05-{document_id}.pdf",
                        original_name="P05 synthetic", file_path="test/p05", file_size=1,
                        file_hash="a" * 64, mime_type="application/pdf"))
        db.add(DocumentArtifact(artifact_id=artifact_id, document_id=document_id, file_hash="a" * 64,
                                preprocess_profile_version="test-p05", status="READY", pages=[]))
        await db.flush()
        db.add(ContentRevision(revision_id=revision_id, document_id=document_id, artifact_id=artifact_id,
                               file_hash="a" * 64, content_digest="b" * 64, adapter_version="test-p05",
                               source_kind="test", change_kind="initial", coverage_digest="c" * 64))
        await db.flush()
        db.add(ParseRun(parse_run_id=run_id, document_id=document_id, content_revision_id=revision_id,
                        status="RUNNING", execution_generation=0, contract_version="reliable-extraction-v2"))
        await db.flush()
        repo = JobRepository(db)
        jobs = await repo.initialize_jobs(run_id, revision_id=revision_id,
                                         fields=[{"field_key": f"f{i:03}", "rule_hash": "d" * 64} for i in range(count)])
        ids = [job.job_id for job in jobs]
    return run_id, revision_id, ids


@pytest.mark.asyncio
async def test_unattempted_fields_are_first_and_yield_automatically_dispatches(sessions):
    run_id, _, ids = await seed(sessions, 10)
    claimed = []
    for _ in range(10):
        async with sessions() as db, db.begin():
            repo = JobRepository(db)
            token = await repo.claim_next(run_id=run_id, owner="fair-test")
            claimed.append(token.job_id)
            await repo.transition(token, "yield", checkpoint={"cursor": 1})
    assert len(set(claimed)) == len(ids)
    async with sessions() as db, db.begin():
        token = await JobRepository(db).claim_next(run_id=run_id, owner="next-round")
        assert token.fence_token > 1
        events = await db.scalar(select(func.count()).select_from(ExecutionOutbox)
                                 .where(ExecutionOutbox.aggregate_id.in_(ids)))
        assert events == 10


@pytest.mark.asyncio
async def test_pause_generation_and_expired_takeover_reject_old_publish(sessions):
    run_id, _, _ = await seed(sessions, 1)
    async with sessions() as db, db.begin():
        a = await JobRepository(db).claim_next(run_id=run_id, owner="a", now=datetime.utcnow()-timedelta(seconds=100), lease_seconds=1)
    async with sessions() as db, db.begin():
        assert await JobRepository(db).requeue_due() >= 1
    async with sessions() as db, db.begin():
        b = await JobRepository(db).claim_next(run_id=run_id, owner="b")
    async with sessions() as db, db.begin():
        with pytest.raises(StaleLeaseError):
            await JobRepository(db).transition(a, "finish", semantic_state="completed")
    async with sessions() as db, db.begin():
        await JobRepository(db).control_run(run_id, "pause")
    async with sessions() as db, db.begin():
        with pytest.raises(StaleLeaseError):
            await JobRepository(db).transition(b, "checkpoint", checkpoint={"stale": True})
    async with sessions() as db, db.begin():
        await JobRepository(db).control_run(run_id, "resume")
        c = await JobRepository(db).claim_next(run_id=run_id, owner="c")
        await JobRepository(db).transition(c, "finish", semantic_state="completed")
        assert await JobRepository(db).aggregate(run_id) == "COMPLETED"


@pytest.mark.asyncio
async def test_failed_resume_preserves_done_and_frozen_checkpoint(sessions):
    run_id, _, ids = await seed(sessions, 2)
    async with sessions() as db, db.begin():
        repo = JobRepository(db)
        a = await repo.claim_next(run_id=run_id, owner="a")
        await repo.transition(a, "finish", semantic_state="completed")
        b = await repo.claim_next(run_id=run_id, owner="b")
        await repo.transition(b, "checkpoint", checkpoint={"cursor": 8})
        await repo.transition(b, "fail", reason="retryable service failure")
        assert await repo.aggregate(run_id) == "FAILED"
    async with sessions() as db, db.begin():
        repo = JobRepository(db)
        await repo.control_run(run_id, "resume", retry_failed=True)
        jobs = (await db.execute(select(ParseFieldJob).where(ParseFieldJob.job_id.in_(ids)))).scalars().all()
        assert sum(job.execution_state == "done" for job in jobs) == 1
        resumed = next(job for job in jobs if job.execution_state == "queued")
        assert resumed.checkpoint == {"cursor": 8}
        assert (await repo.claim_next(run_id=run_id, owner="c")).job_id == b.job_id


@pytest.mark.asyncio
async def test_outbox_concurrent_dedupe_and_lost_dispatch_retry(sessions):
    run_id, _, _ = await seed(sessions, 1)
    key = str(uuid4())

    async def enqueue():
        async with sessions() as db, db.begin():
            event = await JobRepository(db).enqueue_outbox(kind="p05_test", aggregate_id=run_id,
                                                          dedupe_key=key, payload={"run_id": str(run_id)})
            return event.event_id

    first, second = await asyncio.gather(enqueue(), enqueue())
    assert first == second
    async with sessions() as db, db.begin():
        event = await db.get(ExecutionOutbox, first)
        event.dispatch_state = "sending"
        event.updated_at = datetime.utcnow()-timedelta(minutes=10)
    async with sessions() as db, db.begin():
        assert await DispatchOutbox(db).recover_stuck(before=datetime.utcnow()-timedelta(minutes=5)) >= 1
    async with sessions() as db, db.begin():
        event = await db.get(ExecutionOutbox, first)
        assert event.dispatch_state == "failed"


@pytest.mark.asyncio
async def test_no_retry_at_external_wait_is_not_woken_by_scanner(sessions):
    run_id, _, ids = await seed(sessions, 1)
    async with sessions() as db, db.begin():
        repo = JobRepository(db)
        token = await repo.claim_next(run_id=run_id, owner="a")
        await repo.transition(token, "wait", state="waiting_external_quota", retry_at=None)
    async with sessions() as db, db.begin():
        await JobRepository(db).requeue_due()
        assert (await db.get(ParseFieldJob, ids[0])).execution_state == "waiting_external_quota"
