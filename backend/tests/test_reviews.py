import hashlib
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pymupdf
import pytest
from fastapi import HTTPException

from app.api.reviews import _source_pages, _validate_facts, get_field_review_history, review_field
from app.models.reviews import immutable_revision
from app.schemas.reviews import FieldReviewRequest, ReviewedFact


def fact(bbox=None, page=1):
    return ReviewedFact.model_validate({
        "raw_value": "321", "value": "321", "unit": "个", "value_kind": "actual",
        "source_role": "reported", "dimensions": {"workface": "运顺"},
        "evidence": [{"page_number": page, "bbox": bbox, "coordinate_system": "PDF_POINT",
                      "evidence_id": "ev-1"}],
    })


def test_review_request_requires_reason_and_facts_for_correction():
    with pytest.raises(ValueError):
        FieldReviewRequest(action="CORRECT", expected_revision=0, reviewer="a",
                           file_hash="a" * 64, facts=[])
    with pytest.raises(ValueError):
        FieldReviewRequest(action="REJECT", expected_revision=0, reviewer="a",
                           file_hash="a" * 64, reason=" ")
    request = FieldReviewRequest(action="CORRECT", expected_revision=0, reviewer=" a ",
                                 file_hash="a" * 64, reason="人工核对", facts=[fact()])
    assert request.reviewer == "a"


def test_review_evidence_must_be_in_source_page_and_registry():
    field = SimpleNamespace(extraction_detail={"evidence_registry": {
        "ev-1": {"page_number": 1, "bbox": [10, 20, 30, 40]}
    }}, evidence={})
    _validate_facts([fact([10, 20, 30, 40])], field, [{"page_number": 1, "width": 100, "height": 100}])
    with pytest.raises(HTTPException):
        _validate_facts([fact([0, 0, 101, 40])], field,
                        [{"page_number": 1, "width": 100, "height": 100}])


@pytest.mark.asyncio
async def test_history_exposes_automatic_rows_without_mutating_them():
    run_id, document_id, field_id = uuid4(), uuid4(), uuid4()
    run = SimpleNamespace(parse_run_id=run_id, document_id=document_id)
    document = SimpleNamespace(document_id=document_id, file_hash="a" * 64)
    field = SimpleNamespace(field_id=field_id, field_key="count", field_name="数量",
                            field_value="321", unit="个", instances=[], extraction_detail={},
                            evidence={}, validation_status="UNVERIFIED", confidence=None)
    db = Mock(get=AsyncMock(side_effect=[run, document]), execute=AsyncMock())
    result = Mock()
    result.scalar_one_or_none.return_value = field
    history = Mock()
    history.scalars.return_value.all.return_value = []
    db.execute.side_effect = [result, history]
    response = await get_field_review_history(run_id, "count", db=db)
    assert response["automatic"]["field_value"] == "321"
    assert response["latest_revision"] == 0
    assert response["review_status"] == "UNREVIEWED"


@pytest.mark.asyncio
async def test_correct_review_rejects_hash_mismatch_before_source_read():
    run_id, document_id = uuid4(), uuid4()
    run = SimpleNamespace(parse_run_id=run_id, document_id=document_id, status="COMPLETED")
    document = SimpleNamespace(document_id=document_id, file_hash="b" * 64)
    field = SimpleNamespace(field_id=uuid4(), field_key="count", field_name="数量",
                            field_value="321", unit="个", instances=[], extraction_detail={},
                            evidence={}, validation_status="UNVERIFIED", confidence=None)
    db = Mock(get=AsyncMock(side_effect=[run, document]), execute=AsyncMock())
    locked = Mock()
    locked.scalar_one_or_none.return_value = run
    selected = Mock()
    selected.scalar_one_or_none.return_value = field
    db.execute.side_effect = [locked, selected]
    payload = FieldReviewRequest(action="CORRECT", expected_revision=0, reviewer="reviewer",
                                 reason="核对", file_hash="a" * 64, facts=[fact()])
    with pytest.raises(HTTPException) as error:
        await review_field(run_id, "count", payload, db=db)
    assert error.value.status_code == 409


def _db_for_submission(latest=None):
    run_id, document_id = uuid4(), uuid4()
    run = SimpleNamespace(parse_run_id=run_id, document_id=document_id, status="COMPLETED")
    document = SimpleNamespace(document_id=document_id, file_hash="a" * 64)
    field = SimpleNamespace(field_id=uuid4(), field_key="count", field_name="数量",
                            field_value="999", unit="个", instances=[],
                            extraction_detail={"evidence_registry": {"ev-1": {"page_number": 1, "bbox": None}}},
                            evidence={}, validation_status="UNVERIFIED", confidence=None)
    locked = Mock()
    locked.scalar_one_or_none.return_value = run
    selected = Mock()
    selected.scalar_one_or_none.return_value = field
    history = Mock()
    history.scalars.return_value.first.return_value = latest
    db = Mock(get=AsyncMock(side_effect=[run, document]), execute=AsyncMock(side_effect=[locked, selected, history]),
              add=Mock(), flush=AsyncMock())
    async def flush():
        row = db.add.call_args.args[0]
        row.review_id = uuid4()
        row.created_at = datetime.utcnow()
    db.flush.side_effect = flush
    return run_id, field, db


@pytest.mark.asyncio
async def test_correction_appends_revision_and_preserves_automatic_extraction():
    run_id, field, db = _db_for_submission()
    payload = FieldReviewRequest(action="CORRECT", expected_revision=0, reviewer="Reviewer A",
                                 reason="原图数量为321", file_hash="a" * 64, facts=[fact()])
    with patch("app.api.reviews._source_pages", AsyncMock(return_value=[{"page_number": 1, "width": 100, "height": 100}])):
        response = await review_field(run_id, "count", payload, db=db)
    assert response.revision == 1
    assert response.facts[0]["value"] == "321"
    assert response.before_snapshot["automatic"]["field_value"] == "999"
    assert field.field_value == "999"
    assert field.confidence is None
    assert "FOR UPDATE" in str(db.execute.call_args_list[0].args[0])
    db.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_stale_revision_is_rejected_before_source_download():
    run_id, _field, db = _db_for_submission(SimpleNamespace(revision=2))
    payload = FieldReviewRequest(action="REJECT", expected_revision=1, reviewer="Reviewer B",
                                 reason="错值", file_hash="a" * 64)
    with patch("app.api.reviews._source_pages", AsyncMock()) as source:
        with pytest.raises(HTTPException) as error:
            await review_field(run_id, "count", payload, db=db)
    assert error.value.status_code == 409
    source.assert_not_awaited()
    db.add.assert_not_called()


def test_revision_cannot_be_updated_or_deleted():
    with pytest.raises(ValueError, match="append-only"):
        immutable_revision(None, None, None)


@pytest.mark.asyncio
async def test_accept_copies_source_grounded_facts_without_inventing_confidence():
    run_id, field, db = _db_for_submission()
    field.instances = [{"value": "321", "raw_value": "321", "unit": "个",
                        "value_kind": "actual", "source_role": "reported",
                        "dimensions": {"workface": {"value": "运顺", "evidence_ids": ["ev-1"]}},
                        "evidence_ids": ["ev-1"], "quote": "321个"}]
    payload = FieldReviewRequest(action="ACCEPT", expected_revision=0, reviewer="Reviewer A",
                                 file_hash="a" * 64)
    with patch("app.api.reviews._source_pages", AsyncMock(return_value=[{"page_number": 1, "width": 100, "height": 100}])):
        response = await review_field(run_id, "count", payload, db=db)
    assert response.facts[0]["dimensions"] == {"workface": "运顺"}
    assert response.facts[0]["evidence"][0]["evidence_id"] == "ev-1"
    assert "confidence" not in response.facts[0]


@pytest.mark.asyncio
async def test_reject_is_a_separate_audited_decision_with_no_accepted_facts():
    run_id, field, db = _db_for_submission()
    payload = FieldReviewRequest(action="REJECT", expected_revision=0, reviewer="Reviewer A",
                                 reason="原图不支持这个值", file_hash="a" * 64)
    with patch("app.api.reviews._source_pages", AsyncMock(return_value=[{"page_number": 1, "width": 100, "height": 100}])):
        response = await review_field(run_id, "count", payload, db=db)
    assert response.review_status == "REJECTED"
    assert response.facts == []
    assert field.field_value == "999"


@pytest.mark.asyncio
async def test_source_geometry_comes_from_hashed_original_pdf():
    with pymupdf.open() as pdf:
        pdf.new_page(width=123, height=234)
        source = pdf.tobytes()
    doc = SimpleNamespace(file_path="uploads/test.pdf", file_hash=hashlib.sha256(source).hexdigest())
    with patch("app.api.reviews.minio_client.download", return_value=source):
        pages = await _source_pages(doc)
    assert pages == [{"page_number": 1, "width": 123, "height": 234}]
    doc.file_hash = "0" * 64
    with patch("app.api.reviews.minio_client.download", return_value=source):
        with pytest.raises(HTTPException) as error:
            await _source_pages(doc)
    assert error.value.status_code == 409

