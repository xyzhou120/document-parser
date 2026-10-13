from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from app.models.content import ContentRevision
from app.models.source_revisions import SourceRevision
from app.services.content_adapter import ContentAdapter
from app.services.content_service import ContentRevisionError, ContentService


def artifact(*, status="READY", quality=None):
    document_id = uuid4()
    return SimpleNamespace(
        artifact_id=uuid4(),
        document_id=document_id,
        file_hash="f" * 64,
        status=status,
        quality=quality if quality is not None else {
            "page_count": 1,
            "coverage_manifest": {"expected_page_count": 1, "pages": [{"page_number": 1, "scope_complete": True, "regions": []}]},
        },
        pages=[
            {
                "page_number": 1,
                "text": "原生 1.42",
                "native_text": "原生 1.42",
                "text_blocks": [
                    {"text": "原生 1.42", "bbox": [1, 2, 30, 12], "source": "native",
                     "coordinate_system": "PDF_POINT"},
                    {"text": "可解吸 0.18", "bbox": [3, 4, 35, 15], "source": "ocr",
                     "original_locator": {"bbox": [3, 4, 35, 15]}, "coordinate_system": "PDF_POINT"},
                ],
                "ocr_blocks": [{"text": "可解吸 0.18", "bbox": [3, 4, 35, 15]}],
                "tables": [{"table_id": "p1-t1", "title": "测量值", "rows": [["值"]], "cells": [
                    {"row": 0, "col": 0, "text": "值", "bbox": [1, 2, 5, 6]}
                ]}],
            }
        ],
    )


def test_adapter_keeps_raw_and_normalized_sources_with_stable_ids():
    a = artifact()
    first = ContentAdapter().adapt(a)
    second = ContentAdapter().adapt(a)
    assert first.content_digest == second.content_digest
    assert first.coverage_digest == second.coverage_digest
    assert first.pages[0]["text_blocks"] == second.pages[0]["text_blocks"]
    assert first.pages[0]["text_blocks"][0]["raw_text"] == "原生 1.42"
    assert first.pages[0]["text_blocks"][0]["normalized_text"] == "原生 1.42"
    assert first.pages[0]["regions"][0]["source"] == "ocr"
    assert first.pages[0]["tables"][0]["table_id"] == "p1-t1"
    assert first.pages[0]["tables"][0]["cells"][0]["cell_id"]


def test_partial_artifact_cannot_claim_absence():
    a = artifact(status="PARTIAL", quality={"page_count": 2, "failed_pages": [2]})
    content = ContentAdapter().adapt(a)
    assert content.coverage_manifest["status"] == "partial"
    assert content.coverage_complete is False
    assert content.coverage_manifest["unknown_pages"] == [1]
    assert content.coverage_manifest["failed_pages"] == [2]
    assert content.coverage_manifest["can_claim_absence"] is False


def test_legacy_artifact_without_scope_proof_is_unknown():
    a = artifact(quality={})
    content = ContentAdapter().adapt(a)
    assert content.coverage_manifest["status"] == "unknown"
    assert content.coverage_manifest["can_claim_absence"] is False


def full_quality():
    return {"page_count": 1, "coverage_manifest": {"expected_page_count": 1, "pages": [
        {"page_number": 1, "scope_complete": True, "regions": [{"region_id": "full-page", "state": "processed"}]}
    ]}}


def test_only_explicit_complete_region_partition_proves_full_coverage():
    assert ContentAdapter().adapt(artifact(quality=full_quality())).coverage_complete
    q = full_quality()
    q["coverage_manifest"]["pages"][0]["scope_complete"] = False
    assert not ContentAdapter().adapt(artifact(quality=q)).coverage_complete
    for state in ("unknown", "pending", "unreadable", "failed"):
        q = full_quality()
        q["coverage_manifest"]["pages"][0]["regions"][0]["state"] = state
        assert not ContentAdapter().adapt(artifact(quality=q)).coverage_complete
    assert not ContentAdapter().adapt(artifact(status="PARTIAL", quality=full_quality())).coverage_complete
    assert not ContentAdapter().adapt(artifact(quality={"page_count": 1})).coverage_complete


def test_adaptation_preserves_offsets_metadata_and_is_independent_of_input():
    a = artifact()
    a.pages[0]["text"] = "原生\n １.４２"
    a.pages[0]["text_blocks"][0].update(text="１.４２", start_offset=4, end_offset=8)
    a.pages[0]["tables"][0]["continuation_of"] = "previous-table"
    before = deepcopy(a.pages)
    content = ContentAdapter().adapt(a)
    page = content.pages[0]
    assert page["text"] == before[0]["text"]
    block = page["text_blocks"][0]
    assert page["text"][block["start_offset"]:block["end_offset"]] == block["text"]
    assert block["normalized_text"] == "1.42"
    assert page["tables"][0]["continuation_of"] == "previous-table"
    assert a.pages == before
    page["tables"][0]["rows"][0][0] = "changed"
    assert a.pages == before


def test_same_content_digest_for_equivalent_artifacts_and_stable_unaffected_block():
    a, b = artifact(), artifact()
    assert ContentAdapter().adapt(a).content_digest == ContentAdapter().adapt(b).content_digest
    old = ContentAdapter().adapt(a).pages[0]["text_blocks"][1]["block_id"]
    a.pages[0]["text_blocks"].insert(0, {"text": "前文", "bbox": [0, 0, 2, 1]})
    a.pages[0]["text_blocks"][2].update(start_offset=100, end_offset=109)
    assert ContentAdapter().adapt(a).pages[0]["text_blocks"][2]["block_id"] == old


def test_ocr_duplicate_pixel_and_pdf_locators_do_not_duplicate_occurrence():
    a = artifact()
    locator = {"coordinate_system": "PIXEL", "bbox": {"x0": 30, "y0": 40, "x1": 350, "y1": 150}}
    a.pages[0]["text_blocks"][1]["original_locator"] = locator
    a.pages[0]["ocr_blocks"] = [{"text": "可解吸 0.18", "locator": locator}]
    region = ContentAdapter().adapt(a).pages[0]["regions"]
    assert len(region) == 1
    assert region[0]["bbox"] == [3, 4, 35, 15]
    assert region[0]["coordinate_system"] == "PDF_POINT"
    assert region[0]["locator"] == locator


def revision_for(a):
    c = ContentAdapter().adapt(a)
    return ContentRevision(revision_id=uuid4(), document_id=a.document_id, artifact_id=a.artifact_id,
                           file_hash=a.file_hash, adapter_version=c.adapter_version,
                           content_digest=c.content_digest, coverage_digest=c.coverage_digest,
                           coverage_manifest=c.coverage_manifest)


@pytest.mark.asyncio
async def test_read_uses_exact_revision_and_detects_mutation():
    a = artifact()
    revision = revision_for(a)
    db = Mock(get=AsyncMock(side_effect=[revision, a]))
    read = await ContentService().read_revision(db, revision.revision_id)
    assert read.revision is revision
    assert db.get.await_args_list[0].args == (ContentRevision, revision.revision_id)
    a.pages[0]["text"] = "changed"
    db.get.side_effect = [revision, a]
    with pytest.raises(ContentRevisionError, match="no longer matches"):
        await ContentService().read_revision(db, revision.revision_id)
    db.get.side_effect = [None]
    with pytest.raises(ContentRevisionError, match="does not exist"):
        await ContentService().read_revision(db, uuid4())


@pytest.mark.asyncio
async def test_read_rejects_cross_document_and_adapter_mismatch():
    a = artifact()
    revision = revision_for(a)
    revision.adapter_version = "unsupported-future-version"
    db = Mock(get=AsyncMock(side_effect=[revision, a]))
    with pytest.raises(ContentRevisionError, match="unsupported adapter"):
        await ContentService().read_revision(db, revision.revision_id)
    revision.document_id = uuid4()
    db.get.side_effect = [revision, a]
    with pytest.raises(ContentRevisionError, match="identity"):
        await ContentService().read_revision(db, revision.revision_id)


@pytest.mark.asyncio
async def test_pin_is_append_only_transaction_owned_and_maps_source_without_copy():
    a = artifact()
    source = SimpleNamespace(revision_id=uuid4(), document_id=a.document_id, derived_artifact_id=a.artifact_id)
    snapshot = deepcopy(vars(source))
    db = Mock(get=AsyncMock(side_effect=[a, source]), add=Mock(), flush=AsyncMock(), commit=AsyncMock(),
              execute=AsyncMock(return_value=Mock(scalar_one_or_none=Mock(return_value=None))))
    result = await ContentService().pin_revision(db, a, origin_source_revision_id=source.revision_id)
    assert result.revision.artifact_id == a.artifact_id
    assert result.revision.origin_source_revision_id == source.revision_id
    assert vars(source) == snapshot
    assert all(isinstance(call.args[0], ContentRevision) for call in db.add.call_args_list)
    db.commit.assert_not_awaited()
    db.flush.assert_awaited_once()
    assert db.get.await_args_list[0].kwargs["with_for_update"] is True


@pytest.mark.asyncio
async def test_pin_reuses_existing_and_rejects_source_to_wrong_artifact():
    a = artifact()
    revision = revision_for(a)
    db = Mock(get=AsyncMock(return_value=a), add=Mock(), flush=AsyncMock(),
              execute=AsyncMock(return_value=Mock(scalar_one_or_none=Mock(return_value=revision))))
    assert (await ContentService().pin_revision(db, a)).revision is revision
    db.add.assert_not_called()
    source = SimpleNamespace(document_id=a.document_id, derived_artifact_id=uuid4())
    db.get.side_effect = [a, source]
    with pytest.raises(ContentRevisionError, match="derived artifact"):
        await ContentService().pin_revision(db, a, origin_source_revision_id=uuid4())


@pytest.mark.asyncio
async def test_source_mapping_lookup_is_read_only():
    a = artifact()
    revision = revision_for(a)
    source_id = uuid4()
    db = Mock(get=AsyncMock(side_effect=[revision, a]),
              execute=AsyncMock(return_value=Mock(scalar_one_or_none=Mock(return_value=revision))))
    read = await ContentService().get_by_source_revision(db, source_id)
    assert read.revision is revision
    assert source_id in db.execute.call_args.args[0].compile().params.values()
    assert all(call.args[0] is not SourceRevision for call in db.get.await_args_list)

