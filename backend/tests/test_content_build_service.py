from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from app.models.content import ContentBuildJob
from app.models.contracts import Box, Locator, TextBlock
from app.services.content_build_service import ContentBuildService, ServiceUnavailable
from app.services.ocr_correction_service import OCRCorrectionService
from app.services.service_circuit import CircuitPolicy, MemoryCircuitStore, ServiceCircuit


def job(*, checkpoint=None, profile=None):
    return ContentBuildJob(build_id=uuid4(), document_id=uuid4(), file_hash="f" * 64,
                           profile=profile or {"version": "ocr-v1"},
                           checkpoint=checkpoint or {"pages": {}, "regions": {}},
                           dispatch_state="queued", phase="planned", attempt=0, fence_token=0)


def db(job_obj):
    async def get(model, key, **kwargs):
        return job_obj
    return Mock(get=AsyncMock(side_effect=get), flush=AsyncMock(), add=Mock(), commit=AsyncMock(), rollback=AsyncMock())


@pytest.mark.asyncio
async def test_build_resumes_after_midway_failure_and_reuses_success_cache():
    current = job()
    calls = []

    async def ocr(image, page, region):
        calls.append((page, region["region_id"], image))
        if len(calls) == 2:
            raise RuntimeError("temporary OCR outage")
        return {"text": f"页{page}值"}

    service = ContentBuildService(max_region_attempts=3)
    pages = [{"page_number": 1, "regions_scope_complete": True, "regions": [{"region_id": "r1", "image_bytes": b"a"}]},
             {"page_number": 2, "regions_scope_complete": True, "regions": [{"region_id": "r2", "image_bytes": b"b"}]}]
    with pytest.raises(ServiceUnavailable):
        await service.process(db(current), current, pages, ocr=ocr)
    assert current.checkpoint["regions"]["p1:r1"]["state"] == "processed"
    assert current.checkpoint["regions"]["p2:r2"]["state"] == "unknown"
    first_call_count = len(calls)
    result = await service.process(db(current), current, pages, ocr=ocr)
    assert len(calls) == first_call_count + 1
    assert result["regions"]["p1:r1"]["raw_ocr"] == "页1值"
    assert result["coverage_manifest"]["pages"][0]["scope_complete"] is True
    assert result["coverage_manifest"]["pages"][1]["scope_complete"] is True
    assert current.phase == "ready"


@pytest.mark.asyncio
async def test_cache_key_includes_file_image_and_profile_and_empty_is_failure():
    current = job()
    calls = []

    async def ocr(image, page, region):
        calls.append(image)
        return []

    service = ContentBuildService()
    pages = [{"page_number": 1, "regions": [{"region_id": "r", "image_bytes": b"image"}], "regions_scope_complete": True}]
    with pytest.raises(ServiceUnavailable):
        await service.process(db(current), current, pages, ocr=ocr)
    assert len(calls) == 1
    assert current.checkpoint["regions"]["p1:r"]["state"] == "unknown"


@pytest.mark.asyncio
async def test_native_and_unreadable_pages_are_recorded_without_ocr_or_not_found():
    current = job()
    called = False

    async def ocr(*args):
        nonlocal called
        called = True
        return {"text": "unexpected"}

    pages = [{"page_number": 1, "text": "native text", "native_coverage_complete": True},
             {"page_number": 2, "regions": [{"region_id": "u"}], "regions_scope_complete": True}]
    result = await ContentBuildService().process(db(current), current, pages, ocr=ocr)
    assert called is False
    assert result["regions"]["p1:p1:page"]["state"] == "processed"
    assert result["regions"]["p2:u"]["state"] == "unreadable"
    assert result["coverage_manifest"]["pages"][0]["scope_complete"] is True
    assert result["coverage_manifest"]["pages"][1]["scope_complete"] is False


@pytest.mark.asyncio
async def test_circuit_open_yields_waiting_service_and_does_not_consume_region():
    circuit = ServiceCircuit(MemoryCircuitStore(), CircuitPolicy(threshold=1, cooldown_seconds=100))
    current = job()

    async def ocr(*args):
        raise RuntimeError("down")

    with pytest.raises(ServiceUnavailable):
        await ContentBuildService(circuit=circuit, max_region_attempts=5).process(
            db(current), current, [{"page_number": 1, "regions": [{"region_id": "r", "image_bytes": b"x"}]}], ocr=ocr)
    assert current.dispatch_state == "waiting_service"
    with pytest.raises(ServiceUnavailable):
        await ContentBuildService(circuit=circuit, max_region_attempts=5).process(
            db(current), current, [{"page_number": 1, "regions": [{"region_id": "r", "image_bytes": b"x"}]}], ocr=ocr)


def verified_receipt(revision_id, region_id, candidate_id, transcription, image_hash):
    return {"source": "original_image", "revision_id": revision_id, "region_id": region_id, "candidate_id": candidate_id,
            "image_ref": {"image_hash": image_hash}, "verification": {"status": "verified", "transcription": transcription,
            "attributes": {"value": {"evidence_ids": ["e1"], "basis": "image"}}}}


def test_correction_requires_revision_linked_visual_proof_and_keeps_raw_transcription():
    service = OCRCorrectionService()
    revision = str(uuid4())
    image_hash = "img-hash"
    candidate_id = "c1"
    candidate = service.propose(page_number=3, region_id="r", raw_ocr="1.4Z", corrected_transcription="1.42", image_ref={"image_hash": image_hash},
                                bbox=[1, 2, 10, 20], coordinate_system="PIXEL",
                                image_verification="verified", revision_id=revision, candidate_id=candidate_id,
                                verification_receipt=verified_receipt(revision, "r", candidate_id, "1.42", image_hash))
    assert candidate.status == "verified" and candidate.corrected_transcription == "1.42"
    assert candidate.raw_ocr == "1.4Z" and candidate.as_dict()["requires_publication"] is True
    unverified = service.propose(page_number=3, region_id="r", raw_ocr="1.4Z", corrected_transcription="1.42", image_verification="verified")
    assert unverified.status == "proposed" and unverified.corrected_transcription is None


def test_correction_unreadable_conflicting_and_no_progress_candidates_never_guess():
    service = OCRCorrectionService()
    revision = str(uuid4())
    image_hash = "img"
    unreadable = service.propose(page_number=1, region_id="r", raw_ocr="0.18", corrected_transcription="0.81", image_verification="unreadable")
    assert unreadable.status == "unreadable" and unreadable.corrected_transcription is None
    one = service.propose(page_number=1, region_id="r", raw_ocr="1.4Z", corrected_transcription="1.42", image_ref={"image_hash": image_hash}, bbox=[1, 2, 10, 20], coordinate_system="PIXEL", revision_id=revision, candidate_id="c", image_verification="verified", verification_receipt=verified_receipt(revision, "r", "c", "1.42", image_hash))
    two = service.propose(page_number=1, region_id="r", raw_ocr="1.4Z", corrected_transcription="1.48", image_ref={"image_hash": image_hash}, bbox=[1, 2, 10, 20], coordinate_system="PIXEL", revision_id=revision, candidate_id="c", image_verification="verified", verification_receipt=verified_receipt(revision, "r", "c", "1.48", image_hash))
    merged = service.merge_candidates([one, two])
    assert merged[0].status == "conflict" and merged[0].corrected_transcription is None
    assert service.record_stage({}, [unreadable], max_no_progress=1)["correction_state"] == "review_required"


@pytest.mark.asyncio
async def test_thirty_native_pages_small_quantum_progresses_to_last_page():
    current = job()
    service = ContentBuildService()
    pages = [{"page_number": n, "text": f"native page {n}", "native_coverage_complete": True} for n in range(1, 31)]
    counts = []
    for _ in range(10):
        checkpoint = await service.process(db(current), current, pages, max_regions=4)
        count = sum(r["state"] == "processed" for r in checkpoint["regions"].values())
        counts.append(count)
        if current.dispatch_state == "succeeded":
            break
    assert counts == [4, 8, 12, 16, 20, 24, 28, 30]
    assert current.dispatch_state == "succeeded"
    assert checkpoint["coverage_manifest"]["expected_page_count"] == 30
    assert all(p["scope_complete"] for p in checkpoint["coverage_manifest"]["pages"])


@pytest.mark.asyncio
async def test_scan_mixed_rotated_pages_use_renderer_and_preserve_region_identity():
    current = job()
    render = AsyncMock(side_effect=lambda page, region: {"image_bytes": f"page-{page['page_number']}".encode(), "scope_complete": True,
                                                       "image_ref": {"object_key": "source-image"}, "coordinate_system": "PDF_POINT"})
    ocr = AsyncMock(side_effect=lambda image, page, region: {"text": f"OCR {page}"})
    pages = [
        {"page_number": 1, "image_count": 1, "text": ""},
        {"page_number": 2, "image_count": 1, "text": "mixed native", "rotation": 90},
        {"page_number": 3, "text": "hidden text", "native_layer_suspect": True},
    ]
    result = await ContentBuildService().process(db(current), current, pages, renderer=render, ocr=ocr)
    assert ocr.await_count == render.await_count == 3
    assert all(p["scope_complete"] for p in result["coverage_manifest"]["pages"])
    assert len({r["image_hash"] for r in result["regions"].values()}) == 3
    assert result["regions"]["p1:p1:page"]["raw_ocr"] == "OCR 1"
    assert result["regions"]["p2:p2:page"]["raw_ocr"] == "OCR 2"


@pytest.mark.asyncio
async def test_no_region_no_native_proof_failed_ocr_never_becomes_full():
    current = job()
    pages = [{"page_number": 1, "image_count": 1, "text": "", "ocr_status": "failed"}]
    result = await ContentBuildService().process(db(current), current, pages)
    assert result["regions"]["p1:p1:page"]["state"] == "unreadable"
    assert result["coverage_manifest"]["pages"][0]["scope_complete"] is False
    assert current.phase == "partial"


@pytest.mark.asyncio
async def test_same_region_id_on_distinct_pages_and_cache_identity():
    current = job()
    pages = [{"page_number": n, "regions_scope_complete": True, "regions": [{"region_id": "same", "image_bytes": str(n).encode()}]} for n in (1, 2)]
    ocr = AsyncMock(side_effect=lambda image, page, region: {"text": f"{page}"})
    service = ContentBuildService()
    result = await service.process(db(current), current, pages, ocr=ocr)
    await service.process(db(current), current, pages, ocr=ocr)
    assert ocr.await_count == 2
    assert result["regions"]["p1:same"]["raw_ocr"] == "1"
    assert result["regions"]["p2:same"]["raw_ocr"] == "2"
    current.profile = {"version": "v2"}
    await service.process(db(current), current, pages, ocr=ocr)
    assert ocr.await_count == 4


def test_receipt_revision_image_bbox_and_linkage_mismatch_blocks_correction():
    service = OCRCorrectionService()
    revision = str(uuid4())
    kwargs = {"page_number": 1, "region_id": "r", "raw_ocr": "1.4Z", "corrected_transcription": "1.42",
              "image_ref": {"image_hash": "img"}, "revision_id": revision, "candidate_id": "c", "image_verification": "verified",
              "bbox": [1, 2, 10, 20], "coordinate_system": "PIXEL"}
    receipt = verified_receipt(revision, "r", "c", "1.42", "img")
    for key in ("revision_id", "region_id", "candidate_id", "source"):
        bad = {**receipt, key: "wrong"}
        assert service.propose(**kwargs, verification_receipt=bad).status == "proposed"
    assert service.propose(**{**kwargs, "bbox": None}, verification_receipt=receipt).status == "proposed"
    assert service.propose(**{**kwargs, "image_ref": None}, verification_receipt=receipt).status == "proposed"
    assert service.propose(**kwargs, verification_receipt=receipt).status == "verified"
    stage = service.record_stage({}, [service.propose(**kwargs, verification_receipt=receipt)], max_no_progress=1)
    assert service.record_stage(stage, [service.propose(**kwargs, verification_receipt=receipt)], max_no_progress=1)["correction_state"] == "failed_no_progress"


@pytest.mark.asyncio
async def test_retry_limit_stops_service_failures_without_marking_readable():
    current = job()
    pages = [{"page_number": 1, "regions_scope_complete": True, "regions": [{"region_id": "r", "image_bytes": b"x"}]}]
    service = ContentBuildService(max_region_attempts=2)
    ocr = AsyncMock(side_effect=TimeoutError("down"))
    with pytest.raises(ServiceUnavailable):
        await service.process(db(current), current, pages, ocr=ocr)
    result = await service.process(db(current), current, pages, ocr=ocr)
    assert result["regions"]["p1:r"]["state"] == "failed"
    assert current.dispatch_state == "partial"
    await service.process(db(current), current, pages, ocr=ocr)
    assert ocr.await_count == 2


@pytest.mark.parametrize("payload", ["", "  ", [{"text": ""}], [{"text": "  "}], {"state": "processed", "text": ""}, []])
def test_every_empty_ocr_format_requires_explicit_verified_blank(payload):
    with pytest.raises(RuntimeError):
        ContentBuildService._parse_ocr(payload, "r", 1, "hash", {})
    result = ContentBuildService._parse_ocr({"state": "blank", "blank_verified": True}, "r", 1, "hash", {})
    assert result["state"] == "blank"


@pytest.mark.asyncio
async def test_ocr_block_raw_locator_confidence_and_label_survive_checkpoint():
    current = job()
    block = TextBlock(text="1.4Z", confidence=0.73, label="table", locator=Locator(coordinate_system="PIXEL", bbox=Box(x0=10, y0=20, x1=30, y1=40)))
    renderer = AsyncMock(return_value={"image_bytes": b"x", "image_width": 100, "image_height": 200, "scope_complete": True,
                                       "image_ref": {"object_key": "original.png", "image_hash": "h"}})
    result = await ContentBuildService().process(db(current), current,
        [{"page_number": 1, "width": 200, "height": 400, "image_count": 1, "text": ""}],
        renderer=renderer, ocr=AsyncMock(return_value=[block]))
    region = result["regions"]["p1:p1:page"]
    assert region["raw_payload"][0]["locator"]["coordinate_system"] == "PIXEL"
    assert region["blocks"][0]["confidence"] == 0.73
    assert region["blocks"][0]["label"] == "table"
    assert region["blocks"][0]["bbox"] == [20, 40, 60, 80]
    assert region["blocks"][0]["coordinate_system"] == "PDF_POINT"
    assert region["blocks"][0]["original_locator"]["bbox"] == {"x0": 10, "y0": 20, "x1": 30, "y1": 40}
    assert region["location_precision"] == "PAGE"

