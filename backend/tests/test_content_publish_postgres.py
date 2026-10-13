"""P11 lock/CAS/fencing evidence in the explicitly authorized G1 database."""

import asyncio
import os
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.models.content import (
    ContentInvalidation,
    ContentRevision,
    DocumentProcessingHead,
    IndexGeneration,
)
from app.models.database import Document, DocumentArtifact, ParseRun
from app.models.execution import ExecutionOutbox, ParseFieldJob
from app.models.source_revisions import SourceRevision
from app.services.content_publish_service import ContentPublicationError, ContentPublishService
from app.services.execution import DispatchOutbox, JobRepository, StaleLeaseError

AUTHORIZED_DATABASE = "document_parser_v2_g1_20261012_a8d41c6f"


@pytest_asyncio.fixture
async def sessions():
    name = os.environ.get("P11_TEST_DATABASE_NAME")
    if not name:
        pytest.skip("set P11_TEST_DATABASE_NAME to the authorized G1 database")
    if name != AUTHORIZED_DATABASE:
        raise ValueError("P11 test target is not the authorized isolated database")
    engine = create_async_engine(make_url(settings.database.url).set(database=name))
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def artifact(document_id, value="1.4Z"):
    return DocumentArtifact(artifact_id=uuid4(), document_id=document_id, file_hash="f" * 64,
                            preprocess_profile_version="p11-test", status="READY",
                            pages=[{"page_number": 1, "text": value, "native_text": value,
                                    "text_blocks": [{"text": value, "source": "native", "start_offset": 0, "end_offset": len(value)}]}],
                            quality={"page_count": 1, "coverage_manifest": {"expected_page_count": 1, "pages": [
                                {"page_number": 1, "scope_complete": True, "regions": [{"region_id": "page", "state": "processed"}]}]}})


async def seed(sessions, *, ocr=False):
    doc_id = uuid4()
    async with sessions() as db, db.begin():
        db.add(Document(document_id=doc_id, file_name="P11.pdf", original_name="P11 synthetic", file_path="p11/test", file_size=1,
                        file_hash="f" * 64, mime_type="application/pdf"))
        a = artifact(doc_id)
        if ocr:
            a.pages[0]["native_text"] = ""
            a.pages[0]["text_blocks"][0].update(source="ocr", block_id="b1", image_hash="a" * 64,
                original_locator={"coordinate_system": "PIXEL", "bbox": [1, 2, 10, 20]},
                bbox=[1, 2, 10, 20], coordinate_system="PDF_POINT")
        db.add(a)
        await db.flush()
        first = await ContentPublishService().publish_artifact(db, a, expected_cas_version=0, request_id=str(uuid4()))
        rid = first["revision"].revision_id
        run = ParseRun(parse_run_id=uuid4(), document_id=doc_id, artifact_id=a.artifact_id, content_revision_id=rid,
                       contract_version="reliable-extraction-v2", status="RUNNING", execution_generation=0,
                       extraction_profile={"rules": [{"field_key": "gas", "rule_version": 1}], "agent_config": {"thinking_enabled": False}})
        db.add(run)
        await db.flush()
        await JobRepository(db).initialize_jobs(run.parse_run_id, revision_id=rid,
            fields=[{"field_key": "gas", "rule_hash": "d" * 64, "snapshot": {"field_key": "gas", "version": 1}}])
        head = await db.get(DocumentProcessingHead, doc_id)
        head.current_parse_run_id = run.parse_run_id
        generation = IndexGeneration(generation_id=uuid4(), revision_id=rid, config_hash="a" * 64, snapshot_hash="b" * 64,
                                     build_status="succeeded", coverage_status="full", serve_status="eligible", serve_version=1,
                                     indexed_block_manifest={"document_id": str(doc_id), "block_manifest": [{"page_number": 1, "block_id": "b"}]})
        db.add(generation)
        await db.flush()
        head.active_index_generation_id = generation.generation_id
        await db.flush()
        return doc_id, a.artifact_id, rid, run.parse_run_id, generation.generation_id


@pytest.mark.asyncio
async def test_publication_supersedes_old_inflight_and_preserves_frozen_rules(sessions):
    doc, _, parent, run, generation = await seed(sessions)
    async with sessions() as db, db.begin():
        token = await JobRepository(db).claim_next(run_id=run, owner="old-worker")
    async with sessions() as db, db.begin():
        changed = artifact(doc, "1.42")
        db.add(changed)
        await db.flush()
        result = await ContentPublishService().publish_artifact(db, changed, expected_parent_revision_id=parent,
            expected_cas_version=1, expected_invalidation_watermark=0, request_id=str(uuid4()), source_run_id=run,
            changed_ranges=[{"page_number": 1}], change_kind="ocr_correction")
        new_id = result["revision"].revision_id
        successor = result["successor"].parse_run_id
        assert result["head"].active_content_revision_id == new_id
        assert result["head"].current_parse_run_id == run
        assert result["head"].invalidation_watermark == 1
        assert result["successor"].extraction_profile["agent_config"] == {"thinking_enabled": False}
        assert result["successor"].run_mode == "FULL"
        assert result["successor"].extraction_profile["rules"] == [{"field_key": "gas", "rule_version": 1}]
        assert result["successor"].extraction_profile["content_invalidation_watermark"] == 1
        assert result["successor"].extraction_profile["invalidation_watermark"] == 1
        assert result["successor"].extraction_profile["content_cas_version"] == 2
        assert await ContentPublishService().guard_revision(db, document_id=doc,
            revision_id=new_id, expected_invalidation_watermark=1)
    async with sessions() as db, db.begin():
        assert (await db.get(IndexGeneration, generation)).serve_status == "blocked"
        assert (await db.get(IndexGeneration, generation)).serve_version == 2
        assert (await db.get(ParseRun, run)).status == "SUPERSEDED"
        assert (await db.scalar(select(ParseFieldJob).where(ParseFieldJob.run_id == successor))).rule_hash == "d" * 64
        with pytest.raises(StaleLeaseError):
            await JobRepository(db).transition(token, "finish", semantic_state="completed")


@pytest.mark.asyncio
async def test_concurrent_publish_one_cas_winner_and_no_late_pointer(sessions):
    doc, _, parent, run, _ = await seed(sessions)
    artifacts = []
    async with sessions() as db, db.begin():
        for value in ("1.42", "1.43"):
            a = artifact(doc, value)
            db.add(a)
            artifacts.append(a.artifact_id)

    async def publish(aid):
        try:
            async with sessions() as db, db.begin():
                a = await db.get(DocumentArtifact, aid)
                return await ContentPublishService().publish_artifact(db, a, expected_parent_revision_id=parent,
                    expected_cas_version=1, expected_invalidation_watermark=0, request_id=str(uuid4()), source_run_id=run)
        except ContentPublicationError:
            return None

    results = await asyncio.gather(*(publish(a) for a in artifacts))
    assert sum(r is not None for r in results) == 1
    async with sessions() as db, db.begin():
        assert (await db.get(DocumentProcessingHead, doc)).active_content_revision_id != parent
        with pytest.raises(ContentPublicationError):
            await ContentPublishService().publish_parse_run(db, run, expected_cas_version=2,
                expected_invalidation_watermark=1, expected_current_parse_run_id=run)


@pytest.mark.asyncio
async def test_source_revision_replay_is_read_only_and_does_not_duplicate_successor(sessions):
    doc, aid, parent, run, _ = await seed(sessions)
    origin, changed_id = uuid4(), uuid4()
    async with sessions() as db, db.begin():
        changed = artifact(doc, "1.42")
        changed.artifact_id = changed_id
        db.add(changed)
        db.add(SourceRevision(revision_id=origin, document_id=doc, parent_artifact_id=aid, derived_artifact_id=changed_id,
                              revision_type="CORRECT", page_number=1, original_text="1.4Z", replacement_text="1.42", created_by="P11-test"))
    results = []
    for _ in range(2):
        async with sessions() as db, db.begin():
            a = await db.get(DocumentArtifact, changed_id)
            result = await ContentPublishService().publish_artifact(db, a, origin_source_revision_id=origin,
                expected_parent_revision_id=parent, expected_cas_version=1, expected_invalidation_watermark=0,
                source_run_id=run, request_id=f"source:{origin}")
            results.append((result["revision"].revision_id, result["successor"].parse_run_id))
    assert results[0] == results[1]
    async with sessions() as db:
        assert await db.scalar(select(func.count()).select_from(ContentRevision).where(ContentRevision.origin_source_revision_id == origin)) == 1
        assert (await db.get(SourceRevision, origin)).extraction_run_id is None


@pytest.mark.asyncio
async def test_content_outbox_recovers_failed_delivery(sessions):
    doc, _, parent, run, _ = await seed(sessions)
    async with sessions() as db, db.begin():
        changed = artifact(doc, "1.42")
        db.add(changed)
        await db.flush()
        result = await ContentPublishService().publish_artifact(db, changed, expected_parent_revision_id=parent,
            expected_cas_version=1, expected_invalidation_watermark=0, request_id=str(uuid4()), source_run_id=run)
        successor = result["successor"].parse_run_id
        event = await db.scalar(select(ExecutionOutbox).where(ExecutionOutbox.payload["run_id"].as_string() == str(successor)))
        assert event is not None
        event.dispatch_state = "sending"
        await DispatchOutbox(db).mark_failed(event, "broker offline")
        assert event.dispatch_state == "failed"
    async with sessions() as db:
        assert (await db.get(ParseRun, successor)).status == "PLANNED"
        assert await db.scalar(select(func.count()).select_from(ContentInvalidation).where(ContentInvalidation.document_id == doc)) == 1


@pytest.mark.asyncio
async def test_verified_ocr_correction_clones_block_and_publishes_full_successor(sessions):
    doc, _, parent, run, _ = await seed(sessions, ocr=True)
    candidate_id, revision_id, image_hash = "cand-1", str(parent), "a" * 64
    receipt = {"source": "original_image", "revision_id": revision_id, "candidate_id": candidate_id, "region_id": "b1",
               "image_ref": {"image_hash": image_hash},
               "verification": {"status": "verified", "transcription": "1.42",
                                 "attributes": {"value": {"evidence_ids": ["image-e1"], "basis": "wording"}}}}
    candidate = {"candidate_id": candidate_id, "page_number": 1, "region_id": "b1", "raw_ocr": "1.4Z", "image_hash": image_hash,
                 "corrected_transcription": "1.42"}
    async with sessions() as db, db.begin():
        result = await ContentPublishService().publish_verified_ocr(db, run_id=run, candidate=candidate, receipt=receipt,
            request_id="ocr-correction-1")
        successor = result["successor"]
        assert successor is not None
        assert successor.extraction_profile["invalidation_watermark"] == 1
        assert await ContentPublishService().guard_revision(db, document_id=doc,
            revision_id=result["revision"].revision_id, expected_invalidation_watermark=1)
        cloned = await db.get(DocumentArtifact, result["revision"].artifact_id)
        block = cloned.pages[0]["text_blocks"][0]
        assert block["raw_ocr"] == "1.4Z" and block["corrected_transcription"] == "1.42"
        assert block["source"] == "automatic_ocr"
        new_revision_id, successor_id = result["revision"].revision_id, successor.parse_run_id
        assert cloned.pages[0]["text"] == "1.42"
        assert block["original_locator"] == {"coordinate_system": "PIXEL", "bbox": [1, 2, 10, 20]}
    async with sessions() as db, db.begin():
        replay = await ContentPublishService().publish_verified_ocr(db, run_id=run, candidate=candidate, receipt=receipt,
            request_id="ocr-correction-1")
        assert replay["revision"].revision_id == new_revision_id and replay["successor"].parse_run_id == successor_id
        changed_candidate = {**candidate, "corrected_transcription": "1.43"}
        changed_receipt = {**receipt, "verification": {**receipt["verification"], "transcription": "1.43"}}
        with pytest.raises(ContentPublicationError, match="conflicts"):
            await ContentPublishService().publish_verified_ocr(db, run_id=run, candidate=changed_candidate, receipt=changed_receipt)


@pytest.mark.asyncio
async def test_verified_ocr_receipt_mismatch_or_replay_is_rejected(sessions):
    doc, _, _, run, _ = await seed(sessions)
    receipt = {"source": "original_image", "revision_id": str(uuid4()), "candidate_id": "c", "region_id": "b1",
               "image_ref": {"image_hash": "a" * 64}, "verification": {"status": "verified", "transcription": "1.42",
               "attributes": {"value": {"evidence_ids": ["e"], "basis": "wording"}}}}
    candidate = {"candidate_id": "c", "page_number": 1, "region_id": "b1", "raw_ocr": "1.4Z", "image_hash": "a" * 64, "corrected_transcription": "1.42"}
    async with sessions() as db, db.begin():
        with pytest.raises(ContentPublicationError, match="receipt"):
            await ContentPublishService().publish_verified_ocr(db, run_id=run, candidate=candidate, receipt=receipt)


@pytest.mark.asyncio
async def test_publish_pointer_success_then_old_snapshot_cannot_revert(sessions):
    doc, aid, parent, run, _ = await seed(sessions)
    async with sessions() as db, db.begin():
        old = await db.get(ParseRun, run)
        old.status = "COMPLETED"
        field = await db.scalar(select(ParseFieldJob).where(ParseFieldJob.run_id == run))
        field.execution_state = "done"
        field.semantic_state = "completed"
        newer = ParseRun(parse_run_id=uuid4(), document_id=doc, artifact_id=aid, content_revision_id=parent,
                         contract_version="reliable-extraction-v2", status="COMPLETED", execution_generation=0,
                         extraction_profile={"rules": [{"field_key": "gas"}]})
        db.add(newer)
        await db.flush()
        jobs = await JobRepository(db).initialize_jobs(newer.parse_run_id, revision_id=parent,
            fields=[{"field_key": "gas", "rule_hash": "d" * 64}])
        jobs[0].execution_state = "done"
        jobs[0].semantic_state = "completed"
        newer_id = newer.parse_run_id
    async with sessions() as db, db.begin():
        head = await ContentPublishService().publish_parse_run(db, newer_id, expected_cas_version=1,
            expected_invalidation_watermark=0, expected_current_parse_run_id=run)
        assert head.current_parse_run_id == newer_id and head.cas_version == 2
    async with sessions() as db, db.begin():
        with pytest.raises(ContentPublicationError, match="CAS"):
            await ContentPublishService().publish_parse_run(db, run, expected_cas_version=1,
                expected_invalidation_watermark=0, expected_current_parse_run_id=run)
        assert (await db.get(DocumentProcessingHead, doc)).current_parse_run_id == newer_id
        with pytest.raises(ContentPublicationError, match="older run"):
            await ContentPublishService().publish_parse_run(db, run, expected_cas_version=2,
                expected_invalidation_watermark=0, expected_current_parse_run_id=newer_id)


@pytest.mark.asyncio
async def test_effective_and_scoped_invalidations_and_watermark_guard(sessions):
    doc, _, parent, _, _ = await seed(sessions)
    async with sessions() as db, db.begin():
        db.add(ContentInvalidation(document_id=doc, invalidation_kind="field", scope_selector={"scope": "field", "field_key": "gas"},
                                   reason="field only", effective_at=datetime.utcnow() - timedelta(days=1)))
        db.add(ContentInvalidation(document_id=doc, invalidation_kind="content", scope_selector={"scope": "revision", "revision_id": str(parent)},
                                   reason="future", effective_at=datetime.utcnow() + timedelta(days=1)))
        await db.flush()
        await ContentPublishService().guard_revision(db, document_id=doc, revision_id=parent, expected_invalidation_watermark=0)
        with pytest.raises(ContentPublicationError, match="watermark"):
            await ContentPublishService().guard_revision(db, document_id=doc, revision_id=parent, expected_invalidation_watermark=9)
        db.add(ContentInvalidation(document_id=doc, invalidation_kind="content", scope_selector={"content_revision_id": str(parent)},
                                   reason="effective", effective_at=datetime.utcnow() - timedelta(seconds=1)))
        await db.flush()
        with pytest.raises(ContentPublicationError, match="invalidated"):
            await ContentPublishService().guard_revision(db, document_id=doc, revision_id=parent)


@pytest.mark.asyncio
async def test_identical_content_does_not_create_revision_impact_or_successor(sessions):
    doc, _, parent, _, _ = await seed(sessions)
    async with sessions() as db, db.begin():
        same = artifact(doc)
        db.add(same)
        await db.flush()
        result = await ContentPublishService().publish_artifact(db, same, expected_parent_revision_id=parent,
            expected_cas_version=1, expected_invalidation_watermark=0, request_id=str(uuid4()))
        assert result["unchanged"] and result["revision"].revision_id == parent
        assert result["successor"] is None and result["head"].cas_version == 1
        assert await db.scalar(select(func.count()).select_from(ContentRevision).where(ContentRevision.document_id == doc)) == 1


@pytest.mark.asyncio
async def test_failed_successor_transaction_rolls_back_content_head(sessions):
    doc, _, parent, run, _ = await seed(sessions)
    request = str(uuid4())
    with pytest.raises(ContentPublicationError):
        async with sessions() as db, db.begin():
            changed = artifact(doc, "1.42")
            db.add(changed)
            await db.flush()
            service = ContentPublishService()
            service._successor = AsyncMock(side_effect=ContentPublicationError("fault during successor initialization"))
            await service.publish_artifact(db, changed, expected_parent_revision_id=parent, expected_cas_version=1,
                expected_invalidation_watermark=0, request_id=request, source_run_id=run)
    async with sessions() as db:
        head = await db.get(DocumentProcessingHead, doc)
        assert head.active_content_revision_id == parent and head.invalidation_watermark == 0
        assert (await db.get(ParseRun, run)).status == "RUNNING"
        assert await db.scalar(select(func.count()).select_from(ContentRevision).where(ContentRevision.origin_event_id == f"request:{request}")) == 0


@pytest.mark.asyncio
async def test_human_bridge_is_append_only_and_reuses_frozen_rules(sessions):
    from app.schemas.source_revisions import SourceRevisionPayload
    from app.services.source_revision_service import SourceRevisionService
    doc, aid, _, run, generation = await seed(sessions)
    service = SourceRevisionService()
    service._rules_profile = AsyncMock(side_effect=AssertionError("must not read latest rules"))
    async with sessions() as db:
        with patch("app.services.source_revision_service.settings.app.reliable_enabled", True):
            source, successor = await service.create(db, SourceRevisionPayload(
                document_id=doc, parent_artifact_id=aid, expected_file_hash="f" * 64,
                revision_type="CORRECT", page_number=1, start_offset=0, end_offset=4,
                original_text="1.4Z", replacement_text="1.42", created_by="P11-test", parent_run_id=run))
        source_id = source.revision_id
        assert successor is not None
        assert source.extraction_run_id is None
        assert successor.extraction_profile["rules"] == [{"field_key": "gas", "rule_version": 1}]
    async with sessions() as db:
        assert await db.scalar(select(ContentRevision).where(ContentRevision.origin_source_revision_id == source_id)) is not None
        assert (await db.get(IndexGeneration, generation)).serve_status == "blocked"
