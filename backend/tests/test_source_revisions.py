"""Source revision isolation, guards and dispatch handling without external services."""

from copy import deepcopy
from datetime import datetime
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from app.models.database import Document, DocumentArtifact, ParseRun
from app.models.source_revisions import SourceRevision
from app.rules.spec import RuleSpec
from app.schemas.source_revisions import SourceExtractionPayload, SourceRevisionPayload
from app.services.artifact_service import preprocessing_profile
from app.services.parse_service import rule_snapshot
from app.services.source_revision_service import (
    SourceRevisionService,
    correct_page,
    source_state,
    text_hash,
)


@pytest.fixture
def context():
    document = Document(document_id=uuid4(), file_hash="a" * 64, checklist_type="alpha")
    page = {"page_number": 1, "text": "count 12 end", "native_text": "count 12 end",
            "width": 600, "height": 800, "tables": [{"rows": [["12"]]}], "ocr_blocks": [{"text": "12"}],
            "text_blocks": [{"text": "count ", "start_offset": 0, "end_offset": 6},
                            {"text": "12", "start_offset": 6, "end_offset": 8},
                            {"text": " end", "start_offset": 8, "end_offset": 12}]}
    artifact = DocumentArtifact(artifact_id=uuid4(), document_id=document.document_id,
                                file_hash=document.file_hash, status="READY", pages=[page],
                                preprocess_profile_version=preprocessing_profile("rules")["version"],
                                quality={"source_review_status": "UNVERIFIED", "weak_pages": []})
    db = Mock(execute=AsyncMock(return_value=Mock(scalar_one_or_none=Mock(return_value=document))),
              scalar=AsyncMock(return_value=None), get=AsyncMock(return_value=artifact),
              flush=AsyncMock(), commit=AsyncMock(), add=Mock())
    service = SourceRevisionService()
    service._rules_profile = AsyncMock(return_value=rule_snapshot([RuleSpec("gas_count", "数量")], "alpha", "rules"))
    payload = SourceRevisionPayload(document_id=document.document_id, parent_artifact_id=artifact.artifact_id,
                                    expected_file_hash=document.file_hash, page_number=1,
                                    start_offset=6, end_offset=8, original_text="12", replacement_text="173",
                                    original_text_hash=text_hash("12"), created_by="operator", revision_type="CORRECT")
    return service, db, document, artifact, payload


@pytest.mark.asyncio
async def test_correction_clones_artifact_shifts_offsets_and_preserves_original(context):
    service, db, document, artifact, payload = context
    original = deepcopy(artifact.pages)
    revision, run = await service.create(db, payload)
    derived = next(call.args[0] for call in db.add.call_args_list if isinstance(call.args[0], DocumentArtifact))
    assert artifact.pages == original
    assert derived.pages[0]["text"] == "count 173 end"
    assert derived.pages[0]["text_blocks"][-1]["start_offset"] == 9
    assert derived.pages[0]["tables"] == derived.pages[0]["ocr_blocks"] == []
    assert derived.quality["source_review_status"] == "PARTIALLY_REVIEWED"
    assert derived.quality["parent_artifact_id"] == str(artifact.artifact_id)
    assert revision.affected_fields == ["gas_count"] and revision.original_text_hash == text_hash("12")
    assert run is None
    statement = db.execute.call_args_list[0].args[0]
    assert "FOR UPDATE" in str(statement.compile(dialect=postgresql.dialect()))


@pytest.mark.asyncio
async def test_hash_parent_version_and_offset_conflicts_leave_no_revision(context):
    service, db, document, artifact, payload = context
    payload.expected_file_hash = "b" * 64
    with pytest.raises(HTTPException) as exc:
        await service.create(db, payload)
    assert exc.value.status_code == 409
    payload.expected_file_hash = document.file_hash
    payload.original_text = "wrong"
    with pytest.raises(HTTPException) as exc:
        await service.create(db, payload)
    assert exc.value.status_code == 409
    db.add.assert_not_called()
    payload.original_text = "12"
    db.scalar.return_value = SourceRevision(revision_id=uuid4(), parent_artifact_id=artifact.artifact_id,
                                            derived_artifact_id=uuid4())
    with pytest.raises(HTTPException) as exc:
        await service.create(db, payload)
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_confirmation_is_append_only_and_does_not_upgrade_artifact(context):
    service, db, document, artifact, payload = context
    payload.revision_type = "CONFIRM"
    revision, run = await service.create(db, payload)
    assert revision.derived_artifact_id is None and run is None
    assert artifact.quality["source_review_status"] == "UNVERIFIED"
    assert revision.source_review_status == "PARTIALLY_REVIEWED"
    assert len(db.add.call_args_list) == 1


@pytest.mark.asyncio
async def test_unreadable_page_risk_survives_derived_artifact(context):
    service, db, document, artifact, payload = context
    payload.revision_type = "UNREADABLE"
    payload.comment = "扫描污损"
    revision, _ = await service.create(db, payload)
    derived = next(call.args[0] for call in db.add.call_args_list if isinstance(call.args[0], DocumentArtifact))
    assert derived.quality["source_unreadable_pages"] == [1]
    assert derived.quality["weak_pages"] == [1]
    assert derived.pages[0]["source_unreadable"] is True
    assert derived.status == "READY"  # processing completeness and source readability are separate.
    assert revision.source_review_status == "PARTIALLY_REVIEWED"


@pytest.mark.asyncio
async def test_explicit_full_run_fixed_artifact_and_failed_dispatch_visible(context):
    service, db, document, artifact, payload = context
    payload.start_extraction = True
    with patch("app.tasks.parse_document_task.apply_async", side_effect=ConnectionError("offline")):
        revision, run = await service.create(db, payload)
    assert run.status == "FAILED" and run.run_mode == "FULL"
    assert run.requested_field_keys is None
    assert run.artifact_id == revision.derived_artifact_id
    assert run.extraction_profile["reuse_artifact_id"] == str(run.artifact_id)
    assert run.extraction_profile["source_dispatch_failed"] is True
    assert revision.extraction_task_id == str(run.parse_run_id)
    assert db.commit.await_count == 2


@pytest.mark.asyncio
async def test_partial_artifact_never_upgraded_or_extracted(context):
    service, db, document, artifact, payload = context
    artifact.status = "PARTIAL"
    payload.start_extraction = True
    with pytest.raises(HTTPException) as exc:
        await service.create(db, payload)
    assert exc.value.status_code == 409
    db.commit.assert_not_awaited()
    payload.start_extraction = False
    revision, _ = await service.create(db, payload)
    derived = [call.args[0] for call in db.add.call_args_list if isinstance(call.args[0], DocumentArtifact)][-1]
    assert derived.status == "PARTIAL"


@pytest.mark.asyncio
async def test_downstream_state_marks_old_facts_stale_without_accepting_them(context):
    service, db, document, artifact, payload = context
    run = ParseRun(parse_run_id=uuid4(), document_id=document.document_id, artifact_id=artifact.artifact_id)
    revision = SourceRevision(revision_id=uuid4(), parent_artifact_id=artifact.artifact_id,
                              derived_artifact_id=uuid4(), created_at=datetime.utcnow())
    db.execute.return_value = Mock(scalars=Mock(return_value=Mock(all=Mock(return_value=[revision]))))
    state = await source_state(db, run)
    assert state["stale_source"] is True
    assert state["source_review_status"] == "PARTIALLY_REVIEWED"
    assert "VERIFIED" not in state.values()


@pytest.mark.asyncio
async def test_new_raw_preprocessing_does_not_hide_existing_source_revision(context):
    service, db, document, original, payload = context
    revised = DocumentArtifact(artifact_id=uuid4(), document_id=document.document_id,
                               file_hash=document.file_hash, status="READY", pages=original.pages,
                               quality={"source_review_status": "PARTIALLY_REVIEWED"})
    raw_refresh = DocumentArtifact(artifact_id=uuid4(), document_id=document.document_id,
                                   file_hash=document.file_hash, status="READY", pages=original.pages, quality={})
    revision = SourceRevision(revision_id=uuid4(), document_id=document.document_id,
                              parent_artifact_id=original.artifact_id, derived_artifact_id=revised.artifact_id,
                              created_at=datetime.utcnow())
    run = ParseRun(parse_run_id=uuid4(), document_id=document.document_id, artifact_id=raw_refresh.artifact_id)
    db.get.side_effect = lambda model, key: revised if key == revised.artifact_id else raw_refresh
    db.execute.return_value = Mock(scalars=Mock(return_value=Mock(all=Mock(return_value=[revision]))))
    state = await source_state(db, run)
    assert state["stale_source"] is True
    assert state["source_revision_not_applied"] is True
    assert str(revision.revision_id) in state["revision_ids"]


@pytest.mark.asyncio
async def test_retry_submission_failure_reuses_same_run_and_id(context):
    service, db, document, artifact, payload = context
    revision = SourceRevision(revision_id=uuid4(), document_id=document.document_id, extraction_run_id=uuid4())
    run = ParseRun(parse_run_id=revision.extraction_run_id, document_id=document.document_id, status="FAILED",
                   extraction_profile={"source_dispatch_failed": True})
    db.get.side_effect = lambda model, key: revision if model is SourceRevision else run
    db.scalar.return_value = revision
    with patch("app.tasks.parse_document_task.apply_async") as dispatch:
        result = await service.retry(db, revision.revision_id)
        assert result is run and result.status == "PENDING"
        dispatch.assert_called_once_with(args=[str(document.document_id), str(run.parse_run_id)],
                                         task_id=str(run.parse_run_id))
        await service.retry(db, revision.revision_id)
        assert dispatch.call_count == 1


@pytest.mark.asyncio
async def test_extract_request_id_is_idempotent_without_new_run(context):
    service, db, document, artifact, payload = context
    revision = SourceRevision(revision_id=uuid4(), document_id=document.document_id)
    existing = ParseRun(parse_run_id=uuid4(), document_id=document.document_id, status="COMPLETED")
    db.get.return_value = revision
    db.scalar.return_value = existing
    result = await service.extract(db, revision.revision_id, SourceExtractionPayload(request_id=uuid4()))
    assert result is existing
    db.add.assert_not_called()


def test_correct_page_validates_span_and_hash(context):
    service, db, document, artifact, payload = context
    payload.original_text_hash = "0" * 64
    with pytest.raises(HTTPException):
        correct_page(artifact.pages[0], payload, uuid4())


@pytest.mark.asyncio
async def test_preview_has_full_plan_without_saving_or_calling_models(context):
    service, db, document, artifact, payload = context
    preview = await service.preview(db, payload)
    assert preview["mode"] == "FULL" and preview["dependency_precision"] == "UNKNOWN"
    assert preview["model_called"] is False
    assert preview["derived_text"] == "count 173 end"
    assert preview["raw_facts_status"] == "UNVERIFIED"
    db.add.assert_not_called()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_correction_retains_prior_source_override_chain(context):
    service, db, document, artifact, payload = context
    artifact.quality["source_corrections"] = [{"page_number": 2, "replacement_text": "old correction"}]
    payload.start_extraction = True
    with patch("app.tasks.parse_document_task.apply_async"):
        revision, run = await service.create(db, payload)
    assert len(run.extraction_profile["source_corrections"]) == 2
    assert run.extraction_profile["source_corrections"][0]["page_number"] == 2
