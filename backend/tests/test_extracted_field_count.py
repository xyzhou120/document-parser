"""可靠执行结束时，把已经抽出值的字段数写回运行记录。"""

from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.database import ExtractedFieldModel, ParseRun
from app.models.execution import ParseFieldJob
from app.services.execution.job_repository import (
    RELIABLE_CONTRACT,
    JobRepository,
    count_extracted_fields,
    field_has_extracted_value,
    write_extracted_field_counts,
)


def field(key, *, value=None, instances=None, status="completed"):
    return SimpleNamespace(
        field_key=key, field_value=value, instances=instances, validation_status=status,
    )


def test_review_required_values_count_and_absence_does_not():
    rows = [
        field("done", value="12", instances=[{"value": "12"}], status="completed"),
        field("check", value="0.4", instances=[{"value": "0.4"}], status="review_required"),
        field("check", value="0.4", instances=[{"value": "0.4"}], status="review_required"),
        field("empty-review", status="review_required"),
        field("missing", status="not_found"),
        field("open", instances=[{"value": "1"}], status="incomplete"),
        field("blank", status="missing"),
    ]
    assert field_has_extracted_value(rows[1])
    assert not field_has_extracted_value(rows[3])
    assert count_extracted_fields(rows) == 2


class Rows:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class Session:
    def __init__(self, run, jobs, fields):
        self.run = run
        self.jobs = jobs
        self.fields = fields
        self.entities = []

    async def scalar(self, statement):
        assert statement.column_descriptions[0]["entity"] is ParseRun
        return self.run

    async def execute(self, statement):
        entity = statement.column_descriptions[0]["entity"]
        self.entities.append(entity)
        if entity is ParseFieldJob:
            return Rows(self.jobs)
        assert entity is ExtractedFieldModel
        return Rows(self.fields)


def run(status="RUNNING", extracted=0, contract=RELIABLE_CONTRACT):
    return ParseRun(
        parse_run_id=uuid4(), document_id=uuid4(), status=status,
        execution_generation=1, contract_version=contract, fields_extracted=extracted,
        started_at=datetime(2026, 10, 14, 1, 0, 0),
    )


def job(run, key, execution_state, semantic_state):
    return ParseFieldJob(
        job_id=uuid4(), run_id=run.parse_run_id, field_key=key, rule_hash="a" * 64,
        revision_id=uuid4(), execution_state=execution_state, semantic_state=semantic_state,
        checkpoint={}, fence_token=1, progress_seq=1, attempt=1,
    )


@pytest.mark.asyncio
async def test_finished_run_writes_count_without_waiting_for_review():
    current = run()
    jobs = [
        job(current, "done", "done", "completed"),
        job(current, "check", "done", "review_required"),
        job(current, "missing", "done", "not_found"),
    ]
    fields = [
        field("done", value="12", instances=[{"value": "12"}]),
        field("check", value="0.4", instances=[{"value": "0.4"}], status="review_required"),
        field("missing", status="not_found"),
    ]
    for item in fields:
        item.parse_run_id = current.parse_run_id
    session = Session(current, jobs, fields)
    status = await JobRepository(session).aggregate(current.parse_run_id)
    assert status == "COMPLETED_WITH_REVIEW"
    assert current.fields_extracted == 2
    assert ExtractedFieldModel in session.entities
    assert current.completed_at is not None


@pytest.mark.asyncio
async def test_unfinished_or_paused_run_keeps_existing_count():
    current = run(extracted=7)
    session = Session(current, [job(current, "done", "queued", "unknown")], [])
    assert await JobRepository(session).aggregate(current.parse_run_id) == "RUNNING"
    assert current.fields_extracted == 7
    assert session.entities == [ParseFieldJob]

    current.status = "PAUSED"
    session.jobs[0].execution_state = "done"
    session.jobs[0].semantic_state = "completed"
    assert await JobRepository(session).aggregate(current.parse_run_id) == "PAUSED"
    assert current.fields_extracted == 7
    assert session.entities == [ParseFieldJob, ParseFieldJob]


@pytest.mark.asyncio
async def test_list_repairs_only_finished_reliable_runs_still_at_zero():
    legacy = run(status="COMPLETED", extracted=4, contract=None)
    fresh = run(status="COMPLETED_WITH_REVIEW", extracted=0)
    counted = run(status="COMPLETED", extracted=3)
    active = run(status="RUNNING", extracted=0)
    fields = [
        field("done", value="1", instances=[{"value": "1"}]),
        field("check", value="2", status="review_required"),
        field("missing", status="not_found"),
    ]
    for item in fields:
        item.parse_run_id = fresh.parse_run_id
    session = Session(fresh, [], fields)
    await write_extracted_field_counts(session, [legacy, fresh, counted, active], repair_zeros=True)
    assert legacy.fields_extracted == 4
    assert fresh.fields_extracted == 2
    assert counted.fields_extracted == 3
    assert active.fields_extracted == 0
    assert session.entities == [ExtractedFieldModel]
