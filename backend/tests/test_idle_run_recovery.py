"""使用真实映射和查询验证恢复筛选；并发锁语义仍由 PostgreSQL 测试覆盖。"""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app.models.content import ContentRevision  # noqa: F401 - 注册外键元数据
from app.models.database import ParseRun
from app.models.execution import ExecutionOutbox, ParseFieldJob
from app.services.execution import JobRepository


@pytest.mark.parametrize("scenario,expected", [
    ("idle", 1), ("recent_run", 0), ("recent_job", 0), ("recent_wakeup", 0),
    ("pending_wakeup", 0), ("outstanding_wakeup", 0), ("live_lease", 0),
    ("future_backoff", 0), ("completed", 0), ("page_sibling_only", 0),
])
async def test_idle_recovery_uses_persisted_activity(scenario, expected):
    now = datetime.utcnow()
    before = now - timedelta(minutes=10)
    engine = create_engine("sqlite://")
    for table in (ParseRun.__table__, ParseFieldJob.__table__, ExecutionOutbox.__table__):
        table.create(engine)
    try:
        with Session(engine) as session:
            run = ParseRun(parse_run_id=uuid4(), document_id=uuid4(), status="RUNNING",
                execution_generation=0, created_at=before)
            job = ParseFieldJob(job_id=uuid4(), run_id=run.parse_run_id, field_key="a",
                rule_hash="a" * 64, revision_id=uuid4(), execution_state="queued",
                updated_at=before, created_at=before)
            session.add_all([run, job])
            if scenario == "recent_run":
                run.created_at = now
            elif scenario == "recent_job":
                job.updated_at = now
            elif scenario in {"recent_wakeup", "pending_wakeup", "outstanding_wakeup"}:
                session.add(ExecutionOutbox(event_id=uuid4(), kind="field_job_wakeup",
                    aggregate_id=job.job_id, dedupe_key=scenario,
                    dispatch_state="pending" if scenario == "pending_wakeup" else "sent",
                    payload={"dispatch_outstanding": scenario == "outstanding_wakeup"},
                    created_at=before, updated_at=now if scenario == "recent_wakeup" else before))
            elif scenario == "live_lease":
                session.add(ParseFieldJob(job_id=uuid4(), run_id=run.parse_run_id, field_key="b",
                    rule_hash="b" * 64, revision_id=uuid4(), execution_state="running",
                    lease_until=now + timedelta(minutes=1), updated_at=before, created_at=before))
            elif scenario == "future_backoff":
                job.execution_state = "yielded"
                job.next_attempt_at = now + timedelta(minutes=1)
            elif scenario == "completed":
                run.status = "COMPLETED"
            elif scenario == "page_sibling_only":
                run.extraction_profile = {"extraction_strategy": "page_batch_v1", "coordinator_field_key": "b"}
            session.commit()

            def scalars(statement):
                # 同时编译 PostgreSQL 查询，确保 JSON 和相关子查询可用于实际方言。
                statement.compile(dialect=postgresql.dialect())
                return session.scalars(statement)

            db = SimpleNamespace(scalars=AsyncMock(side_effect=scalars),
                                 scalar=AsyncMock(side_effect=session.scalar))
            repo = JobRepository(db)
            repo.enqueue_outbox = AsyncMock()
            assert await repo.recover_idle_runs(now=now) == expected
            assert repo.enqueue_outbox.await_count == expected
            if expected:
                assert repo.enqueue_outbox.await_args.kwargs["payload"]["run_id"] == str(run.parse_run_id)
    finally:
        engine.dispose()
