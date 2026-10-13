"""方案 C1：扫描页通过区域 OCR 进入清单代理（2026-10-10）。

不访问网络：OCR 服务和 PDF 渲染器替换为模拟对象，其答案结构已根据 DGC 扫描表单验证
（每个布局区域一项，包含像素 bbox 和文本）。
"""

import asyncio
import struct
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

from app.models.contracts import Box, Locator, TextBlock
from app.parsers.ocr_client import PaddleOCRClient
from app.services import artifact_service
from app.services.artifact_service import preprocessing_profile
from app.services.parse_service import ParseService

OLD_CHECKLIST_VERSION = "pp-8c90388bb69835d08e8f4340dea08ed85378f1fd"


def png(width, height):
    header = (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR"
              + struct.pack(">II", width, height))
    return header + b"\x00" * 16


def client_without_init():
    client = PaddleOCRClient.__new__(PaddleOCRClient)
    client.base_url = "http://ocr.test:7000"
    client.api_key = None
    client.timeout = 5
    return client


def test_region_items_keep_text_pixel_box_and_layout_label_without_a_score():
    payload = [
        {"label": "doc_title", "bbox": [100, 40, 500, 80], "image": None, "content": "乔子梁煤矿"},
        {"label": "text", "bbox": [293, 1586, 604, 1620], "image": None,
         "content": "可解吸瓦斯量 $ m^{3}/t $：1.02"},
        {"label": "figure", "bbox": [10, 10, 20, 20], "image": None, "content": "   "},
        {"label": "table", "bbox": [50, 300, 400, 380], "image": None,
         "content": "残余瓦斯含量(m3/t) 2.46\n压力(MPa) 0.19"},
    ]
    blocks = client_without_init()._parse_regions(payload, page_number=21)
    assert [b.text for b in blocks] == [
        "乔子梁煤矿", "可解吸瓦斯量 $ m^{3}/t $：1.02", "残余瓦斯含量(m3/t) 2.46\n压力(MPa) 0.19"]
    assert blocks[1].label == "text" and blocks[2].label == "table"
    assert blocks[2].locator.bbox.x0 == 50 and blocks[2].locator.bbox.y1 == 380
    assert blocks[2].locator.coordinate_system == "PIXEL" and blocks[2].locator.page_number == 21
    assert all(b.confidence is None for b in blocks)  # 不伪造分数。
    assert blocks[0].location_status == "ocr_region"


def test_region_endpoint_is_the_vl_ocr_route(monkeypatch):
    calls = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return [{"label": "text", "bbox": [1, 2, 30, 40], "content": "残余瓦斯含量 2.23"}]

    class FakeAsyncClient:
        def __init__(self, timeout):
            calls["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, files, headers):
            calls["url"] = url
            calls["field"] = list(files)
            return FakeResponse()

    monkeypatch.setattr("app.parsers.ocr_client.httpx.AsyncClient", FakeAsyncClient)
    blocks = asyncio.run(client_without_init().recognize_regions(png(100, 100), 3, 100, 100))
    assert calls["url"] == "http://ocr.test:7000/vl_ocr"
    assert calls["field"] == ["file"]
    assert blocks and blocks[0].text == "残余瓦斯含量 2.23"


def test_region_failure_is_an_empty_answer_not_a_fabricated_block(monkeypatch):
    class BrokenAsyncClient:
        def __init__(self, timeout):
            pass

        async def __aenter__(self):
            raise RuntimeError("connection refused")

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr("app.parsers.ocr_client.httpx.AsyncClient", BrokenAsyncClient)
    assert asyncio.run(client_without_init().recognize_regions(png(10, 10), 1, 10, 10)) == []


def test_checklist_manifest_carries_the_ocr_identity_and_a_new_version():
    checklist = preprocessing_profile("checklist_agent")
    assert checklist["manifest"]["ocr_endpoint"] == "/vl_ocr"
    assert checklist["manifest"]["ocr_policy"].startswith("region-ocr")
    assert checklist["version"] != OLD_CHECKLIST_VERSION
    rules = preprocessing_profile("rules")
    assert "ocr_endpoint" not in rules["manifest"]  # 规则产物保持自身标识。


def test_checklist_ocr_reads_scanned_pages_by_regions_and_records_status():
    service = ParseService.__new__(ParseService)
    service.pdf_parser = Mock(
        inspect_pages=Mock(return_value=[
            SimpleNamespace(page_number=1, needs_ocr=False),
            SimpleNamespace(page_number=2, needs_ocr=True),
            SimpleNamespace(page_number=3, needs_ocr=True),
        ]),
        get_page_image=Mock(return_value=png(1654, 2339)),
        dpi=150,
    )
    region_blocks = [TextBlock(
        text="可解吸瓦斯量 $ m^{3}/t $：1.02", confidence=None,
        location_status="ocr_region", label="text",
        locator=Locator(coordinate_system="PIXEL", page_number=2,
                        bbox=Box(x0=293, y0=1586, x1=604, y1=1620)))]
    service.ocr_client = Mock(
        recognize=AsyncMock(side_effect=AssertionError("line OCR must not run for checklist")),
        recognize_regions=AsyncMock(side_effect=lambda image, n, w, h: [] if n == 3 else region_blocks),
    )
    service._log = AsyncMock()
    pages = [
        {"page_number": 1, "width": 600, "height": 800, "text": "背景说明"},
        {"page_number": 2, "width": 600, "height": 800, "text": ""},
        {"page_number": 3, "width": 600, "height": 800, "text": ""},
    ]
    blocks_by_page, failed = asyncio.run(service._ocr_scanned_pages(
        AsyncMock(), uuid4(), pages, Path("/tmp/none.pdf"), True))

    assert service.ocr_client.recognize.await_count == 0
    assert [p["ocr_status"] for p in pages] == ["not_needed", "ok", "failed"]
    assert failed == {3}
    page2 = pages[1]
    assert page2["text"].endswith("可解吸瓦斯量 $ m^{3}/t $：1.02")
    block = page2["text_blocks"][0]
    assert block["source"] == "ocr" and block["label"] == "text"
    # 根据渲染图像尺寸将像素框转换为 PDF 点坐标，例如 293 * 600 / 1654。
    assert abs(block["bbox"][0] - 293 * 600 / 1654) < 1e-6
    assert abs(block["bbox"][3] - 1620 * 800 / 2339) < 1e-6
    assert blocks_by_page[2][0]["confidence"] is None


def test_line_ocr_is_still_used_by_the_rules_engine():
    service = ParseService.__new__(ParseService)
    service.pdf_parser = Mock(
        inspect_pages=Mock(return_value=[SimpleNamespace(page_number=1, needs_ocr=True)]),
        get_page_image=Mock(return_value=png(100, 100)),
        dpi=150,
    )
    service.ocr_client = Mock(
        recognize=AsyncMock(return_value=[TextBlock(text="钻孔45个", confidence=0.97)]),
        recognize_regions=AsyncMock(side_effect=AssertionError("rules must not use regions")),
    )
    service._log = AsyncMock()
    pages = [{"page_number": 1, "width": 100, "height": 100, "text": ""}]
    asyncio.run(service._ocr_scanned_pages(AsyncMock(), uuid4(), pages, Path("/tmp/x.pdf"), False))
    assert service.ocr_client.recognize.await_count == 1
    assert pages[0]["ocr_status"] == "ok"
    assert artifact_service.PREPROCESS_PROFILE_VERSION  # 共享常量的含义保持不变。
