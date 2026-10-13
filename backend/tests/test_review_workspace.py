"""可选复核工作区读取真实 SQL，且不修改来源记录。"""

from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.api.review_workspace import list_review_workspace, router
from app.models.database import Base, Document, ExtractedFieldModel, ParseRun
from app.models.reviews import FieldReviewRevision
from app.storage import get_db


class LocalDB:
    def __init__(self, session):
        self.session = session

    async def execute(self, statement):
        return self.session.execute(statement)

    async def scalar(self, statement):
        return self.session.scalar(statement)


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        document = Document(document_id=uuid4(), file_name="sample.pdf", original_name="原文.pdf",
                            file_path="sample.pdf", file_size=100, file_hash="a" * 64,
                            mime_type="application/pdf", mine_name="甲矿", checklist_type="alpha")
        run = ParseRun(parse_run_id=uuid4(), document_id=document.document_id,
                       checklist_type="alpha", status="COMPLETED")
        session.add_all([document, run])
        session.flush()
        for key in ("count", "missing", "other"):
            session.add(ExtractedFieldModel(
                parse_run_id=run.parse_run_id, document_id=document.document_id,
                field_key=key, field_name=key, extraction_method="agent_extract",
                field_value=None, required=key == "missing",
                validation_status="missing" if key == "missing" else "passed",
                instances=[] if key == "missing" else [{"value": "120", "issues": [{"code": "UNIT_UNVERIFIED"}]}],
            ))
        session.flush()
        yield LocalDB(session), run, document
    engine.dispose()


def review(db, run, action, revision, key="count"):
    row = FieldReviewRevision(
        parse_run_id=run.parse_run_id, document_id=run.document_id, field_key=key,
        revision=revision, previous_revision=revision - 1, action=action, reviewer="复核人",
        reason="核对原图", file_hash="a" * 64, before_snapshot={}, facts=[],
        created_at=datetime(2026, 10, 9) + timedelta(minutes=revision),
    )
    db.session.add(row)
    db.session.flush()
    return row


async def query(db, **kwargs):
    return await list_review_workspace(state=kwargs.get("state", "UNREVIEWED"),
        checklist_type=kwargs.get("checklist_type"), mine_name=kwargs.get("mine_name"),
        page=kwargs.get("page", 1), page_size=kwargs.get("page_size", 20), db=db)


@pytest.mark.asyncio
async def test_latest_revision_partition_and_missing_fields_are_preserved(db):
    local, run, _ = db
    review(local, run, "ACCEPT", 1)
    latest = review(local, run, "REJECT", 2)
    rows = await query(local)
    assert rows["total"] == 2
    missing = next(item for item in rows["items"] if item["field_key"] == "missing")
    assert missing["required"] and missing["fact_count"] == 0
    rejected = await query(local, state="REJECTED")
    assert rejected["total"] == 1
    assert rejected["items"][0]["latest_review"]["review_id"] == str(latest.review_id)
    assert rejected["items"][0]["fact_count"] == 1  # 自动候选仍然可见。
    assert (await query(local, state="HUMAN_REVIEWED"))["total"] == 0
    review(local, run, "CORRECT", 3)
    assert (await query(local, state="REJECTED"))["total"] == 0
    assert (await query(local, state="HUMAN_REVIEWED"))["total"] == 1
    source = local.session.scalar(select(ExtractedFieldModel).where(ExtractedFieldModel.field_key == "count"))
    assert source.validation_status == "passed" and source.field_value is None


@pytest.mark.asyncio
async def test_filter_pagination_risks_and_run_partition(db):
    local, run, document = db
    another = ParseRun(parse_run_id=uuid4(), document_id=document.document_id,
                       checklist_type="beta", status="COMPLETED")
    local.session.add(another)
    local.session.add(ExtractedFieldModel(parse_run_id=another.parse_run_id,
        document_id=document.document_id, field_key="count", field_name="另一个运行",
        field_value="173", extraction_method="keyword", validation_status="passed"))
    local.session.flush()
    review(local, run, "REJECT", 1)
    beta = await query(local, checklist_type="beta", mine_name="甲")
    assert beta["total"] == 1 and beta["items"][0]["fact_count"] == 1
    alpha = await query(local, checklist_type="alpha", page_size=1)
    assert alpha["total"] == 2 and len(alpha["items"]) == 1
    all_alpha = await query(local, checklist_type="alpha")
    other = next(item for item in all_alpha["items"] if item["field_key"] == "other")
    assert "UNIT_UNVERIFIED" in other["risk_codes"]
    assert (await query(local, mine_name="不存在"))["total"] == 0


@pytest.mark.asyncio
async def test_in_progress_runs_are_not_actionable(db):
    local, run, _ = db
    run.status = "RUNNING"
    local.session.flush()
    assert (await query(local))["items"] == []


def test_http_request_rejects_invalid_state_and_pagination_before_query():
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: None
    with TestClient(app) as client:
        for query in ("state=VERIFIED", "page=0", "page_size=101", "page_size=0"):
            assert client.get(f"/api/v1/review-workspace?{query}").status_code == 422
