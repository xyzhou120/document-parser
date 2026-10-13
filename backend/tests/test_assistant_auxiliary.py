from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from app.models.database import ParseRun
from app.services.assistant.card_service import extract_card
from app.services.assistant.compare_service import CompareService
from app.services.assistant.feedback_service import FeedbackService
from app.services.content_adapter import ContentAdapter


def content(*, complete=True):
    artifact = SimpleNamespace(artifact_id=uuid4(), document_id=uuid4(), file_hash="a" * 64,
                               status="READY", quality={"page_count": 1}, pages=[{"page_number": 1, "text": "第一章 现场测量值 1.42", "native_text": "第一章 现场测量值 1.42",
                               "text_blocks": [{"text": "第一章 现场测量值 1.42", "block_id": "b1", "bbox": [1, 2, 100, 20], "source": "native", "coordinate_system": "PDF_POINT"}]}])
    value = ContentAdapter().adapt(artifact)
    if complete:
        value = value.__class__(**{**value.__dict__, "coverage_manifest": {**value.coverage_manifest, "status": "full", "can_claim_absence": True}})
    return value


def test_card_source_summary_has_citations_and_never_reads_facts():
    result = extract_card(content(), uuid4(), uuid4())
    assert result["summary"]
    assert result["citations"][0]["block_id"] == "b1"
    assert result["outline"]["source"] == "source_excerpts"
    assert "第一章" in result["outline"]["topics"][0]["text"]
    assert "facts" not in result["summary"]


def test_card_partial_version_surfaces_risk_and_bad_bbox_is_preserved_only_as_source():
    result = extract_card(content(complete=False), uuid4(), None)
    assert result["risks"]
    assert result["citations"][0]["coordinate_system"] == "PDF_POINT"


@pytest.mark.asyncio
async def test_compare_is_explicit_read_only_and_marks_version_mismatch():
    service = CompareService()
    message = SimpleNamespace(message_id=uuid4(), session_id=uuid4(), revision_id=uuid4(), generation_id=None,
                              execution_state="succeeded", answer="原文答案", claims=[], citations=[])
    session = SimpleNamespace(session_id=message.session_id, document_id=uuid4())
    run = ParseRun(parse_run_id=uuid4(), document_id=session.document_id, content_revision_id=uuid4(), extraction_profile={}, status="COMPLETED")
    db = Mock(get=AsyncMock(side_effect=[message, session, run]), execute=AsyncMock(side_effect=[Mock(scalars=Mock(return_value=Mock(all=Mock(return_value=[])))), Mock(scalars=Mock(return_value=Mock(all=Mock(return_value=[])))), Mock(scalars=Mock(return_value=Mock(all=Mock(return_value=[]))))]))
    result = await service.compare(db, message.message_id, parse_run_id=run.parse_run_id)
    assert result["version_mismatch"] and result["source_answer"]["answer"] == "原文答案"
    assert result["writes_facts"] is False and result["automatic_acceptance"] is False
    assert result["fact_view"]["facts"] == []
    db.add.assert_not_called()
    db.commit.assert_not_called()


@pytest.mark.asyncio
async def test_feedback_service_requires_fixed_source_and_pending_record():
    # 被引用的固定块不存在时，服务应在插入前拒绝定位器。
    from app.schemas.assistant_auxiliary import AssistantFeedbackRequest, FeedbackLocator
    from app.services.content_service import RevisionContent
    message = SimpleNamespace(message_id=uuid4(), session_id=uuid4(), revision_id=uuid4(), generation_id=None,
                              answer="a", claims=[], citations=[], question="q", execution_state="succeeded", checkpoint={})
    session = SimpleNamespace(session_id=message.session_id, document_id=uuid4())
    revision = SimpleNamespace(document_id=session.document_id, file_hash="a" * 64, content_digest="b" * 64)
    fixed = content()
    service = FeedbackService(content=SimpleNamespace(read_revision=AsyncMock(return_value=RevisionContent(revision, fixed))))
    db = Mock(get=AsyncMock(side_effect=[message, session, revision, None]), scalar=AsyncMock(return_value=None), execute=AsyncMock())
    payload = AssistantFeedbackRequest(request_id="f-1", expected_correction={"value": "1.4"}, reason="digit", submitted_by="u",
                                       locator=FeedbackLocator(page_number=1, block_id="missing", quote="no"))
    with pytest.raises(ValueError, match="原文块"):
        await service.submit(db, message.message_id, payload)
    db.execute.assert_not_awaited()
