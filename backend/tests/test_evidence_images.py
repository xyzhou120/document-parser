"""来源渲染产物复用和定位器来源检查。"""

import asyncio
import hashlib
from types import SimpleNamespace

import pymupdf
import pytest

from app.services.evidence_images import EvidenceImages
from app.services.evidence_images import InvalidEvidenceRegion, clip_page_bbox


def test_page_boundary_crop_preserves_overlap_and_rejects_unusable_regions():
    page = {"width": 200, "height": 300}
    assert clip_page_bbox(page, [-1, 5, 201, 301]) == [0, 5, 200, 300]
    assert clip_page_bbox(page, None) is None
    for bbox in ([210, 0, 220, 20], [100, 0, 10, 20], [0, 0, float("nan"), 1], [False, 0, 10, 20]):
        with pytest.raises(InvalidEvidenceRegion):
            clip_page_bbox(page, bbox)


class MemoryStorage:
    def __init__(self, objects):
        self.objects = dict(objects)
        self.downloads = []
        self.uploads = []

    def exists(self, key):
        return key in self.objects

    def download(self, key):
        self.downloads.append(key)
        return self.objects[key]

    def upload(self, key, data, content_type):
        self.objects[key] = data
        self.uploads.append((key, content_type))


@pytest.fixture
def source_setup(tmp_path, monkeypatch):
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=200, height=300)
        page.insert_text((20, 40), "Original 321 holes")
        source = pdf.tobytes()
    storage = MemoryStorage({"original/report.pdf": source})
    document = SimpleNamespace(
        document_id="doc-1",
        file_hash=hashlib.sha256(source).hexdigest(),
        file_path="original/report.pdf",
    )
    pages = [{"page_number": 1, "width": 200, "height": 300}]
    monkeypatch.setattr("app.services.evidence_images.minio_client", storage)
    monkeypatch.setattr(
        "app.services.evidence_images.settings",
        SimpleNamespace(agent=SimpleNamespace(render_dpi=144, region_render_dpi=144)),
    )
    return tmp_path, storage, document, pages


def test_cache_hit_does_not_download_original_pdf(source_setup):
    workspace, storage, document, pages = source_setup
    first = EvidenceImages(document, pages, workspace)
    image = asyncio.run(first.get(1))
    storage.downloads.clear()
    second = EvidenceImages(document, pages, workspace)
    cached = asyncio.run(second.get(1))
    assert cached == image
    assert document.file_path not in storage.downloads
    assert len(storage.downloads) == 2
    assert storage.downloads[0].startswith("artifacts/")
    assert second.pdf_path is None
    assert next(iter(second.receipts.values()))["cache_status"] == "HIT"


def test_cache_miss_checks_source_hash_renders_and_persists(source_setup):
    workspace, storage, document, pages = source_setup
    images = EvidenceImages(document, pages, workspace)
    png = asyncio.run(images.get(1))
    assert png.startswith(b"\x89PNG")
    assert storage.downloads == [document.file_path]
    assert len(storage.uploads) == 2
    key, content_type = storage.uploads[0]
    assert storage.objects[key] == png
    assert content_type == "image/png"
    assert next(iter(images.receipts.values()))["cache_status"] == "MISS"
    assert images.pdf_path.read_bytes() == storage.objects[document.file_path]


def test_cache_miss_rejects_changed_original_before_render(source_setup, monkeypatch):
    workspace, storage, document, pages = source_setup
    document.file_hash = "0" * 64
    images = EvidenceImages(document, pages, workspace)
    rendered = []
    monkeypatch.setattr(images, "_render", lambda *args: rendered.append(args))
    with pytest.raises(ValueError, match="hash"):
        asyncio.run(images.get(1))
    assert not rendered
    assert not storage.uploads
    assert not images.receipts


@pytest.mark.parametrize(
    "bbox",
    [
        [-1, 0, 100, 100],
        [0, 0, 201, 100],
        [0, 0, 100, 301],
        [0, 0, 0, 10],
        [0, 0, 10],
        [False, 0, 10, 10],
        [0, 0, float("nan"), 10],
    ],
)
def test_invalid_bbox_never_downloads_or_renders(source_setup, monkeypatch, bbox):
    workspace, storage, document, pages = source_setup
    images = EvidenceImages(document, pages, workspace)
    rendered = []
    monkeypatch.setattr(images, "_render", lambda *args: rendered.append(args))
    with pytest.raises(ValueError):
        asyncio.run(images.get(1, bbox))
    assert not rendered
    assert not storage.downloads
    assert not storage.uploads


def test_receipts_preserve_exact_actual_page_and_region_precision(source_setup):
    workspace, storage, document, pages = source_setup
    images = EvidenceImages(document, pages, workspace)
    page_png = asyncio.run(images.get(1))
    clip = [20.5, 15.25, 180.75, 90.5]
    region_png = asyncio.run(images.get(1, clip))
    assert len(images.receipts) == 2
    receipts = list(images.receipts.values())
    page, region = receipts
    assert page["location_precision"] == "page"
    assert page["bbox"] == [0.0, 0.0, 200.0, 300.0]
    assert region["location_precision"] == "region"
    assert region["bbox"] == clip
    assert region["page_number"] == 1
    assert region["coordinate_system"] == "PDF_POINT"
    assert region["source_review_status"] == "UNVERIFIED"
    assert region["source_file_hash"] == document.file_hash
    assert region["image_hash"] == hashlib.sha256(region_png).hexdigest()
    assert region["render_profile"]["dpi"] == 144
    assert storage.downloads == [document.file_path]
    assert len(region_png) != len(page_png)


def test_corrupt_cached_image_is_regenerated_from_verified_source(source_setup):
    workspace, storage, document, pages = source_setup
    first = EvidenceImages(document, pages, workspace)
    original = asyncio.run(first.get(1))
    key = next(iter(first.receipts))
    storage.objects[key] = b"corrupt image"
    storage.downloads.clear()
    second = EvidenceImages(document, pages, workspace)
    assert asyncio.run(second.get(1)) == original
    assert document.file_path in storage.downloads
    assert second.receipts[key]["cache_integrity"] == "REGENERATED"


def test_existing_local_pdf_is_hash_checked_before_render(source_setup):
    workspace, storage, document, pages = source_setup
    source = workspace / "wrong.pdf"
    source.write_bytes(b"wrong source")
    images = EvidenceImages(document, pages, workspace, pdf_path=source)
    with pytest.raises(ValueError, match="hash"):
        asyncio.run(images.get(1))
    assert not storage.uploads


def test_reused_renderer_returns_receipt_for_requested_crop(source_setup):
    workspace, storage, document, pages = source_setup
    images = EvidenceImages(document, pages, workspace)
    first_clip, second_clip = [0, 0, 100, 100], [20, 20, 180, 200]
    first, first_receipt = asyncio.run(images.get_with_receipt(1, first_clip))
    second, second_receipt = asyncio.run(images.get_with_receipt(1, second_clip))
    repeated, repeated_receipt = asyncio.run(images.get_with_receipt(1, first_clip))
    assert repeated == first
    assert repeated_receipt["object_key"] == first_receipt["object_key"]
    assert repeated_receipt["bbox"] == first_clip
    assert second_receipt["bbox"] == second_clip
    assert second_receipt["image_hash"] == hashlib.sha256(second).hexdigest()
    assert storage.downloads.count(document.file_path) == 1
