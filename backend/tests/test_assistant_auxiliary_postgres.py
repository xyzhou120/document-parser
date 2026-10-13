"""P18 可选能力与问答、提取保持独立。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from test_content_publish_postgres import seed
from test_content_publish_postgres import sessions as isolated_sessions

from app.models.assistant import DocumentCard, QAMessage, QASession
from app.models.assistant_feedback import AssistantFeedbackSample
from app.models.database import ParseRun
from app.schemas.assistant_auxiliary import AssistantFeedbackRequest
from app.services.assistant.card_service import CardService
from app.services.assistant.feedback_service import FeedbackService
from app.services.assistant.index_repository import IndexRepository
from app.services.assistant.qa_service import QARepository, QAService


@pytest_asyncio.fixture
async def db_sessions():
    async for factory in isolated_sessions.__wrapped__():
        yield factory


async def qa_message(factory, document_id):
    service = QAService(QARepository(factory), IndexRepository(factory), answerer=None)
    session = await service.create_session(document_id=document_id, request_id=str(uuid4()))
    message, _ = await service.submit_message(session["session_id"], request_id=str(uuid4()), question="原文在哪里？")
    return message


@pytest.mark.asyncio
async def test_no_card_still_accepts_qa_and_card_failure_never_changes_message_or_parse(db_sessions):
    doc, _, _, run, _ = await seed(db_sessions)
    async with db_sessions() as db:
        card = await CardService().get(db, doc)
        assert card["status"] == "not_generated"
    message = await qa_message(db_sessions, doc)
    async with db_sessions() as db, db.begin():
        card = await CardService().request(db, doc, request_id="create")
    failure = CardService(content=SimpleNamespace(read_revision=AsyncMock(side_effect=RuntimeError("failed source read"))))
    result = await failure.build(db_sessions, card["card_id"])
    assert result["status"] == "failed"
    async with db_sessions() as db:
        assert (await db.get(QAMessage, message["message_id"])).execution_state == "queued"
        assert (await db.get(ParseRun, run)).status == "RUNNING"
        assert (await db.get(QASession, message["session_id"])).status == "active"


@pytest.mark.asyncio
async def test_card_build_fixed_revision_source_citations_and_request_replay(db_sessions):
    doc, _, parent, _, _ = await seed(db_sessions)
    async with db_sessions() as db, db.begin():
        card = await CardService().request(db, doc, request_id="same-request")
        replay = await CardService().request(db, doc, request_id="same-request")
        assert replay["card_id"] == card["card_id"]
    result = await CardService().build(db_sessions, card["card_id"])
    assert result["status"] == "succeeded" and result["revision_id"] == str(parent)
    assert result["summary"] == "1.4Z"
    assert result["citations"][0]["quote"] == "1.4Z"
    async with db_sessions() as db:
        assert await db.scalar(select(func.count()).select_from(DocumentCard).where(DocumentCard.document_id == doc)) == 1


@pytest.mark.asyncio
async def test_feedback_pending_replay_conflict_does_not_publish_or_edit_answer(db_sessions):
    doc, _, _, run, _ = await seed(db_sessions)
    async with db_sessions() as db:
        if await db.scalar(text("SELECT to_regclass('assistant_feedback_samples') IS NOT NULL")) is not True:
            pytest.skip("P11 G1 database has not applied the P18 feedback migration")
    message = await qa_message(db_sessions, doc)
    payload = AssistantFeedbackRequest(request_id="offline-feedback", expected_correction={"answer": "需要原图核对"},
                                       reason="识别疑似错误", submitted_by="operator")
    async with db_sessions() as db, db.begin():
        first = await FeedbackService().submit(db, message["message_id"], payload)
    async with db_sessions() as db, db.begin():
        replay = await FeedbackService().submit(db, message["message_id"], payload)
        assert replay["feedback_id"] == first["feedback_id"]
        assert replay["adjudication_status"] == "pending" and replay["facts_changed"] is False
    async with db_sessions() as db, db.begin():
        with pytest.raises(ValueError, match="request_id"):
            await FeedbackService().submit(db, message["message_id"], payload.model_copy(update={"expected_correction": {"answer": "冲突修正"}}))
    async with db_sessions() as db:
        audit = await db.get(AssistantFeedbackSample, UUID(first["feedback_id"]))
        assert audit.expected_correction == payload.expected_correction
        assert audit.snapshot["automatic_original"]["question"] == message["question"]
        assert audit.adjudication_status == "pending" and audit.snapshot["counts_as_automatic"] is False
        assert (await db.get(QAMessage, message["message_id"])).answer is None
        assert (await db.get(ParseRun, run)).status == "RUNNING"
    for statement in ("UPDATE assistant_feedback_samples SET submitted_by='changed' WHERE feedback_id=:id",
                      "DELETE FROM assistant_feedback_samples WHERE feedback_id=:id"):
        with pytest.raises(DBAPIError, match="append-only"):
            async with db_sessions() as db, db.begin():
                await db.execute(text(statement), {"id": UUID(first["feedback_id"])})
