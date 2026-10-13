"""方案 B：整页视图和区域裁剪分别按配置的 DPI 渲染。

使用测试中生成的真实单页 PDF 和内存中的 MinIO 模拟对象（无缓存命中），
因此可在不访问网络的情况下端到端执行渲染路径。
"""

import asyncio
import hashlib
from types import SimpleNamespace

import pymupdf

from app.services import evidence_images as ei


class NoCacheStorage:
    def exists(self, key):
        return False

    def download(self, key):
        raise AssertionError("cache must not be read in this test")

    def upload(self, key, data, content_type):
        return None


def test_whole_page_and_region_use_separate_dpi(tmp_path, monkeypatch):
    pdf = pymupdf.open()
    pdf.new_page(width=200, height=300)
    pdf_path = tmp_path / "report.pdf"
    pdf.save(pdf_path)
    pdf.close()
    source = pdf_path.read_bytes()

    monkeypatch.setattr(ei, "minio_client", NoCacheStorage())
    document = SimpleNamespace(
        document_id="doc-1",
        file_hash=hashlib.sha256(source).hexdigest(),
        file_path="original/report.pdf",
    )
    pages = [{"page_number": 1, "width": 200, "height": 300}]
    images = ei.EvidenceImages(document, pages, tmp_path, pdf_path=pdf_path,
                               dpi=120, region_dpi=180)

    page_png = asyncio.run(images.get(1))
    region_png = asyncio.run(images.get(1, [20.5, 15.25, 180.75, 90.5]))

    page_receipt, region_receipt = list(images.receipts.values())
    assert page_receipt["location_precision"] == "page"
    assert page_receipt["render_profile"]["dpi"] == 120
    assert region_receipt["location_precision"] == "region"
    assert region_receipt["render_profile"]["dpi"] == 180
    # 两种渲染结果都必须是真实 PNG 字节，而不是占位数据。
    assert page_png.startswith(b"\x89PNG") and region_png.startswith(b"\x89PNG")
