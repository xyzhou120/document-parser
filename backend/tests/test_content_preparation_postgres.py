"""Initial content preparation through repeated P09 quantums, in isolated G1."""

import hashlib
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from test_content_publish_postgres import sessions as isolated_sessions

from app.models.content import ContentBuildJob, ContentRevision
from app.models.contracts import Box, Locator, TextBlock
from app.models.database import Document, DocumentArtifact, ParseRun
from app.models.execution import ExecutionOutbox, ParseFieldJob
from app.services.content_build_service import ContentBuildService
from app.services.content_preparation_service import (
    ContentPreparationError,
    ContentPreparationService,
)
from app.services.content_service import ContentService
from app.services.service_circuit import MemoryCircuitStore, ServiceCircuit


@pytest_asyncio.fixture
async def db_sessions():
    async for factory in isolated_sessions.__wrapped__():
        yield factory


class FakeParser:
    def __init__(self, count=30, scanning=False):
        self.count, self.scanning, self.extract_count = count, scanning, 0

    def extract_pages(self, path):
        self.extract_count += 1
        return [{"page_number": i, "text": "" if self.scanning else f"native {i}", "native_text": "" if self.scanning else f"native {i}",
                 "image_count": 1 if self.scanning else 0, "width": 100, "height": 200, "text_blocks": [], "tables": []}
                for i in range(1, self.count + 1)]

    def inspect_pages(self, path):
        return [SimpleNamespace(page_number=i, needs_ocr=self.scanning, is_scan=self.scanning) for i in range(1, self.count+1)]


class NativeParser(FakeParser):
    def __init__(self):
        super().__init__(count=2, scanning=False)

    def inspect_pages(self, path):
        return [SimpleNamespace(page_number=1, needs_ocr=False, is_scan=False),
                SimpleNamespace(page_number=2, needs_ocr=True, is_scan=True)]

    def extract_pages(self, path):
        return [{"page_number": 1, "text": "native 1", "native_text": "native 1", "image_count": 0, "width": 100, "height": 200, "text_blocks": [], "tables": []},
                {"page_number": 2, "text": "", "native_text": "", "image_count": 1, "width": 100, "height": 200, "text_blocks": [], "tables": []}]


async def document(factory, source):
    did = uuid4()
    async with factory() as db, db.begin():
        db.add(Document(document_id=did, file_name="prepare.pdf", original_name="prepare synthetic", file_path="fake/preparation",
                        file_size=len(source), file_hash=hashlib.sha256(source).hexdigest(), mime_type="application/pdf"))
    return did


def preparation(parser):
    return ContentPreparationService(parser=parser, builder=ContentBuildService(circuit=ServiceCircuit(MemoryCircuitStore())),
                                     cache_dir=Path(".tmp-preparation-cache") / str(uuid4()))


@pytest.mark.asyncio
async def test_thirty_scan_pages_publish_after_quantums_and_replay_is_idempotent(db_sessions):
    source = b"fake pdf 30 pages"
    did = await document(db_sessions, source)
    parser = FakeParser(scanning=True)
    service = preparation(parser)
    resolver = AsyncMock(return_value=source)
    render = AsyncMock(side_effect=lambda page, region: {"image_bytes": f"page-{page['page_number']}".encode(), "scope_complete": True,
                                                       "whole_page": True, "image_width": 100, "image_height": 200})
    ocr = AsyncMock(side_effect=lambda image, page, region: [TextBlock(text=f"值 {page}", confidence=.9,
        locator=Locator(coordinate_system="PIXEL", bbox=Box(x0=1,y0=2,x1=30,y1=20)))])
    frozen = {"rules": [{"field_key": "gas", "rule_hash": "a" * 64}], "model_config": {"model": "frozen-model"}}
    async with db_sessions() as db, db.begin():
        build = await service.prepare_build(db, did, profile=frozen, request_id="r1")
    outcomes = []
    for _ in range(10):
        result = await service.run_build(db_sessions, UUID(build["build_id"]), source_resolver=resolver,
                                         renderer=render, ocr=ocr, max_regions=4)
        outcomes.append(result["status"])
        if result["status"] == "published":
            break
    assert outcomes == ["yielded"] * 7 + ["published"]
    assert resolver.await_count == 1 and ocr.await_count == 30 and parser.extract_count == 1
    again = await service.run_build(db_sessions, UUID(build["build_id"]), source_resolver=resolver, renderer=render, ocr=ocr)
    assert again["run_id"] == result["run_id"] and ocr.await_count == 30
    async with db_sessions() as db:
        pinned = await ContentService().read_revision(db, UUID(result["revision_id"]))
        assert pinned.content.coverage_complete
        assert pinned.content.pages[-1]["ocr_text"] == "值 30"
        assert pinned.content.pages[-1]["text_blocks"][0]["bbox"] == [1,2,30,20]
        run = await db.get(ParseRun, UUID(result["run_id"]))
        assert run.artifact_id == pinned.revision.artifact_id
        assert run.extraction_profile["model_config"] == frozen["model_config"]
        assert await db.scalar(select(func.count()).select_from(ParseRun).where(ParseRun.document_id == did)) == 1
        assert await db.scalar(select(func.count()).select_from(DocumentArtifact).where(DocumentArtifact.document_id == did)) == 1
        assert await db.scalar(select(func.count()).select_from(ParseFieldJob).where(ParseFieldJob.run_id == run.parse_run_id)) == 1
        assert await db.scalar(select(func.count()).select_from(ExecutionOutbox).where(ExecutionOutbox.kind == "content.build.execute", ExecutionOutbox.aggregate_id == UUID(build["build_id"]))) == 8


@pytest.mark.asyncio
async def test_hash_mismatch_and_request_conflict_never_publish(db_sessions):
    source = b"expected"
    did = await document(db_sessions, source)
    service = preparation(FakeParser(count=1))
    async with db_sessions() as db, db.begin():
        build = await service.prepare_build(db, did, profile={"rules": []}, request_id="r")
        with pytest.raises(ContentPreparationError, match="request_id"):
            await service.prepare_build(db, did, profile={"rules": [{"field_key": "changed"}]}, request_id="r")
    with pytest.raises(ContentPreparationError, match="hash"):
        await service.run_build(db_sessions, UUID(build["build_id"]), source_resolver=lambda _: b"wrong")
    async with db_sessions() as db:
        assert await db.scalar(select(func.count()).select_from(ContentRevision).where(ContentRevision.document_id == did)) == 0


def test_mixed_rotation_and_hidden_layers_never_receive_native_coverage_attestation():
    pages = [{"page_number": n, "text": "native", "image_count": 0, "native_text": "native"} for n in (1,2,3,4)]
    pages[0]["image_count"] = 1
    pages[1]["rotation"] = 90
    pages[2]["native_layer_suspect"] = True
    inspections = [SimpleNamespace(page_number=n, needs_ocr=False, is_scan=False) for n in (1,2,3,4)]
    prepared = ContentPreparationService._page_inputs(deepcopy(pages), inspections)
    assert [p["native_coverage_complete"] for p in prepared] == [False,False,False,True]


@pytest.mark.asyncio
async def test_native_page_skips_ocr_scan_page_failure_stays_unreadable(db_sessions):
    source = b"native-and-scan"
    did = await document(db_sessions, source)
    parser = NativeParser()
    service = preparation(parser)
    resolver = AsyncMock(return_value=source)
    render = AsyncMock(return_value={"image_bytes": b"scan", "scope_complete": True, "whole_page": True,
                                     "image_width": 100, "image_height": 200})
    ocr = AsyncMock(side_effect=RuntimeError("OCR down"))
    async with db_sessions() as db, db.begin():
        build = await service.prepare_build(db, did, profile={"rules": []}, request_id="native-scan")
    result = await service.run_build(db_sessions, UUID(build["build_id"]), source_resolver=resolver,
                                     renderer=render, ocr=ocr, max_regions=2)
    assert result["status"] in {"waiting_service", "partial"}
    assert ocr.await_count == 1 and render.await_count == 1
    async with db_sessions() as db:
        job = await db.get(ContentBuildJob, UUID(build["build_id"]))
        assert job.phase != "published"
