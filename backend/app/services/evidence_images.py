"""Persist and reuse source page/crop images, independently of field/model rules."""

import asyncio
import hashlib
import json
import math
from pathlib import Path

import pymupdf

from app.config import settings
from app.storage import minio_client


class EvidenceImages:
    def __init__(self, document, pages: list[dict], workspace: Path,
                 pdf_path: Path | None = None, dpi: int | None = None,
                 region_dpi: int | None = None):
        self.document = document
        self.pages = {p["page_number"]: p for p in pages}
        self.workspace = workspace
        self.pdf_path = pdf_path
        self.receipts: dict[str, dict] = {}
        # Plan B: whole-page views and region crops have separate resolutions.
        # Whole pages are the expensive case; crops keep higher resolution for small digits.
        self.dpi = dpi if dpi is not None else settings.agent.render_dpi
        self.region_dpi = (region_dpi if region_dpi is not None
                           else getattr(settings.agent, "region_render_dpi", 180))

    async def get(self, page_number: int, bbox: list[float] | None = None) -> bytes:
        data, _ = await self.get_with_receipt(page_number, bbox)
        return data

    async def get_with_receipt(self, page_number: int,
                               bbox: list[float] | None = None) -> tuple[bytes, dict]:
        """Return the receipt for this exact crop, even when the renderer is reused."""
        page = self.pages.get(page_number)
        if page is None:
            raise ValueError("页面不存在")
        width, height = float(page["width"]), float(page["height"])
        clip = list(bbox) if bbox is not None else [0.0, 0.0, width, height]
        if len(clip) != 4 or any(isinstance(x, bool) or not math.isfinite(float(x)) for x in clip):
            raise ValueError("无效裁图坐标")
        x0, y0, x1, y1 = map(float, clip)
        if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
            raise ValueError("裁图必须在对应页面范围内")
        clip = [x0, y0, x1, y1]
        dpi = self.dpi if bbox is None else self.region_dpi
        render = {"algorithm": "source-render-v1", "pymupdf": pymupdf.VersionBind,
                  "dpi": dpi, "page": page_number, "bbox": clip}
        signature = hashlib.sha256(json.dumps(render, sort_keys=True).encode()).hexdigest()
        key = f"artifacts/{self.document.document_id}/{self.document.file_hash}/images/{signature}.png"
        receipt_key = key + ".json"
        hit = (await asyncio.to_thread(minio_client.exists, key)
               and await asyncio.to_thread(minio_client.exists, receipt_key))
        invalid_cache = False
        if hit:
            data = await asyncio.to_thread(minio_client.download, key)
            try:
                stored = json.loads(await asyncio.to_thread(minio_client.download, receipt_key))
                hit = (stored.get("source_file_hash") == self.document.file_hash
                       and stored.get("render_profile") == render
                       and stored.get("image_hash") == hashlib.sha256(data).hexdigest()
                       and data.startswith(b"\x89PNG\r\n\x1a\n"))
            except (ValueError, TypeError, AttributeError):
                hit = False
            invalid_cache = not hit
        if not hit:
            if self.pdf_path is None:
                source = await asyncio.to_thread(minio_client.download, self.document.file_path)
                if hashlib.sha256(source).hexdigest() != self.document.file_hash:
                    raise ValueError("原文件 hash 不一致，禁止生成证据图像")
                self.pdf_path = self.workspace / "agent-source.pdf"
                self.pdf_path.write_bytes(source)
            elif hashlib.sha256(await asyncio.to_thread(self.pdf_path.read_bytes)).hexdigest() != self.document.file_hash:
                raise ValueError("本地原文件 hash 不一致，禁止生成证据图像")
            data = await asyncio.to_thread(self._render, page_number, clip, dpi)
            await asyncio.to_thread(minio_client.upload, key, data, "image/png")
            stored = {"source_file_hash": self.document.file_hash, "render_profile": render,
                      "image_hash": hashlib.sha256(data).hexdigest()}
            await asyncio.to_thread(minio_client.upload, receipt_key,
                                    json.dumps(stored, sort_keys=True).encode(), "application/json")
        self.receipts[key] = {
            "object_key": key, "image_hash": hashlib.sha256(data).hexdigest(),
            "source_file_hash": self.document.file_hash, "document_id": str(self.document.document_id),
            "page_number": page_number, "bbox": clip, "coordinate_system": "PDF_POINT",
            "coordinate_origin": "MUPDF_ROTATED_TOP_LEFT",
            "page_rotation": page.get("rotation", 0),
            "page_width": width, "page_height": height,
            "location_precision": "region" if bbox is not None else "page",
            "render_profile": render, "cache_status": "HIT" if hit else "MISS",
            "cache_integrity": "REGENERATED" if invalid_cache else "HASH_CHECKED",
            "source_review_status": "UNVERIFIED",
        }
        return data, self.receipts[key]

    def _render(self, page_number: int, clip: list[float], dpi: int) -> bytes:
        with pymupdf.open(self.pdf_path) as doc:
            return bytes(doc[page_number - 1].get_pixmap(dpi=dpi, clip=pymupdf.Rect(clip)).tobytes("png"))
