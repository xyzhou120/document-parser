"""
OCR服务客户端（PaddleOCR）

服务契约（来自 172.26.1.84:7000 的 OpenAPI）：
    POST /ocr            普通 OCR
    POST /vl_ocr         视觉模型 OCR（带版面）
    POST /structure_ocr  结构化 OCR
均为 multipart 上传字段名 image/file。
"""

import logging

import httpx

from app.config import settings
from app.models.contracts import Box, Locator, TextBlock

logger = logging.getLogger(__name__)


class PaddleOCRClient:
    """PaddleOCR服务客户端"""

    def __init__(self):
        ocr_config = settings.models.get("ocr")
        if not ocr_config:
            raise ValueError("ocr 配置缺失")
        # 配置项可能是 "host:port/ocr" 或完整 URL，这里统一归一化为根地址
        self.base_url = self._normalize_base(ocr_config.base_url)
        self.api_key = ocr_config.api_key
        self.timeout = ocr_config.timeout
        self.endpoint = "/ocr"

    @staticmethod
    def _normalize_base(raw: str) -> str:
        """把配置里的地址归一化为 http://host:port 形式

        原配置为 "172.26.1.84:7000/ocr"，代码里又拼了一次 "/ocr"，
        会造成 /ocr/ocr 的双路径错误，这里统一剥掉路径部分。
        """
        if not raw:
            return ""
        url = raw.strip()
        if not url.startswith(("http://", "https://")):
            url = f"http://{url}"
        # 去掉协议后的路径，只保留 host:port
        without_scheme = url.split("://", 1)[1]
        host_port = without_scheme.split("/", 1)[0]
        scheme = url.split("://", 1)[0]
        return f"{scheme}://{host_port}"

    async def recognize(
        self, image: bytes, page_number: int, image_width: int, image_height: int
    ) -> list[TextBlock]:
        """
        调用OCR识别图片

        Args:
            image: 图片字节
            page_number: 页码
            image_width: 图片宽度
            image_height: 图片高度

        Returns:
            文本块列表
        """
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                headers = {}
                if self.api_key:
                    headers["Authorization"] = f"Bearer {self.api_key}"

                # 服务端表单字段名为 file（见其 OpenAPI 定义），传 image 会返回 422
                response = await client.post(
                    f"{self.base_url}{self.endpoint}",
                    files={"file": ("page.png", image, "image/png")},
                    headers=headers,
                )
                response.raise_for_status()
                payload = response.json()

            return self._parse_blocks(payload, page_number, image_width, image_height)

        except httpx.HTTPStatusError as e:
            logger.error(
                f"OCR HTTP error on page {page_number}: {e.response.status_code} "
                f"{e.response.text[:200]}"
            )
            return []
        except Exception as e:
            logger.error(f"OCR failed on page {page_number}: {e}")
            return []

    # 方案 C1：清单代理通过区域接口读取扫描页；接口为每个布局区域返回
    # 像素 bbox 和文本（已在 DGC 扫描表单上验证）。
    REGION_ENDPOINT = "/vl_ocr"

    async def recognize_regions(
        self, image: bytes, page_number: int, image_width: int, image_height: int
    ) -> list[TextBlock]:
        """供清单代理使用的区域 OCR：文本块携带像素 bbox 和布局标签。

        此接口不提供分数，因此文本块置信度保持未知。错误返回空列表，
        由调用方记录为页面失败。
        """
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                headers = {}
                if self.api_key:
                    headers["Authorization"] = f"Bearer {self.api_key}"
                response = await client.post(
                    f"{self.base_url}{self.REGION_ENDPOINT}",
                    files={"file": ("page.png", image, "image/png")},
                    headers=headers,
                )
                response.raise_for_status()
                payload = response.json()
            return self._parse_regions(payload, page_number)
        except httpx.HTTPStatusError as e:
            logger.error(f"OCR region HTTP error on page {page_number}: {e.response.status_code}")
            return []
        except Exception as e:
            logger.error(f"OCR region failed on page {page_number}: {e}")
            return []

    def _parse_regions(self, payload, page_number: int) -> list[TextBlock]:
        if isinstance(payload, dict):
            for key in ("regions", "result", "results", "blocks", "data"):
                if isinstance(payload.get(key), list):
                    payload = payload[key]
                    break
        if not isinstance(payload, list):
            return []
        blocks: list[TextBlock] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            text = str(item.get("content") or item.get("text") or "").strip()
            if not text:
                continue
            bbox = self._to_box(item.get("bbox"))
            label = str(item.get("label") or "").strip() or None
            blocks.append(
                TextBlock(
                    text=text,
                    locator=(
                        Locator(coordinate_system="PIXEL", bbox=bbox, page_number=page_number)
                        if bbox else None
                    ),
                    location_status="ocr_region" if bbox else "unavailable",
                    confidence=None,
                    label=label,
                )
            )
        return blocks

    def _parse_blocks(
        self, payload, page_number: int, image_width: int, image_height: int
    ) -> list[TextBlock]:
        """解析 OCR 服务返回，兼容多种常见结构"""
        blocks: list[TextBlock] = []
        if payload is None:
            return blocks

        # 该服务（PaddleOCR 封装）实际返回扁平结构：
        #   {"rec_texts": [...], "rec_scores": [...]}  可能还有 rec_boxes / rec_polys
        if isinstance(payload, dict) and payload.get("rec_texts"):
            texts = payload.get("rec_texts") or []
            scores = payload.get("rec_scores") or []
            boxes = payload.get("rec_boxes") or payload.get("rec_polys") or []
            for index, text in enumerate(texts):
                content = str(text or "").strip()
                if not content:
                    continue
                try:
                    confidence = float(scores[index]) if index < len(scores) else 0.0
                except (TypeError, ValueError):
                    confidence = 0.0
                bbox = self._to_box(boxes[index]) if index < len(boxes) else None
                blocks.append(
                    TextBlock(
                        text=content,
                        locator=(
                            Locator(
                                coordinate_system="PIXEL",
                                bbox=bbox,
                                page_number=page_number,
                            )
                            if bbox
                            else None
                        ),
                        location_status="ocr_block" if bbox else "unavailable",
                        confidence=max(0.0, min(1.0, confidence)),
                    )
                )
            return blocks

        # 兼容: {"result": [...]} / {"data": [...]} / {"texts": [...]} / 直接是列表
        items = payload
        if isinstance(payload, dict):
            for key in ("result", "results", "data", "texts", "lines", "blocks"):
                if key in payload and isinstance(payload[key], list):
                    items = payload[key]
                    break
            else:
                # 纯文本返回
                text = payload.get("text") or payload.get("content")
                if isinstance(text, str) and text.strip():
                    return [
                        TextBlock(
                            text=text.strip(),
                            locator=None,
                            location_status="unavailable",
                            confidence=0.0,
                        )
                    ]
                items = []

        if not isinstance(items, list):
            return blocks

        for index, item in enumerate(items):
            text, confidence, bbox = self._parse_item(item, index)
            if not text:
                continue
            blocks.append(
                TextBlock(
                    text=text,
                    locator=(
                        Locator(
                            coordinate_system="PIXEL",
                            bbox=bbox,
                            page_number=page_number,
                        )
                        if bbox
                        else None
                    ),
                    location_status="ocr_block" if bbox else "unavailable",
                    confidence=max(0.0, min(1.0, confidence)),
                )
            )
        return blocks

    @staticmethod
    def _parse_item(item, index: int):
        """从单个 OCR 结果项里取出文本、置信度与框"""
        text, confidence, bbox = "", 0.0, None

        if isinstance(item, str):
            return item.strip(), 0.0, None

        if isinstance(item, dict):
            text = str(
                item.get("text")
                or item.get("rec_text")
                or item.get("transcription")
                or item.get("content")
                or ""
            ).strip()
            try:
                raw_confidence = item.get("confidence", item.get("score", 0.0))
                confidence = float(raw_confidence) if raw_confidence is not None else 0.0
            except (TypeError, ValueError):
                confidence = 0.0
            box = item.get("bbox") or item.get("box") or item.get("points")
            bbox = PaddleOCRClient._to_box(box)
            return text, confidence, bbox

        if isinstance(item, (list, tuple)):
            # 常见格式: [[x0,y0],[x1,y1],[x2,y2],[x3,y3]], ("文本", 0.98)
            if item and isinstance(item[0], str):
                text = item[0].strip()
                if len(item) > 1:
                    try:
                        confidence = float(item[1])
                    except (TypeError, ValueError):
                        confidence = 0.0
                return text, confidence, None
            if len(item) == 2 and isinstance(item[1], (list, tuple)):
                points = item[0]
                text = str(item[1][0]).strip() if item[1] else ""
                try:
                    confidence = float(item[1][1]) if len(item[1]) > 1 else 0.0
                except (TypeError, ValueError):
                    confidence = 0.0
                return text, confidence, PaddleOCRClient._to_box(points)

        return text, confidence, bbox

    @staticmethod
    def _to_box(raw):
        """把多种多边形/矩形格式归一化为 Box"""
        if not raw:
            return None
        try:
            if isinstance(raw, dict):
                if {"x0", "y0", "x1", "y1"} <= set(raw.keys()):
                    return Box(
                        x0=float(raw["x0"]),
                        y0=float(raw["y0"]),
                        x1=float(raw["x1"]),
                        y1=float(raw["y1"]),
                    )
                if {"left", "top", "width", "height"} <= set(raw.keys()):
                    left, top = float(raw["left"]), float(raw["top"])
                    return Box(
                        x0=left,
                        y0=top,
                        x1=left + float(raw["width"]),
                        y1=top + float(raw["height"]),
                    )
            if isinstance(raw, (list, tuple)) and len(raw) >= 2:
                if isinstance(raw[0], (list, tuple)):
                    xs = [float(p[0]) for p in raw if len(p) >= 2]
                    ys = [float(p[1]) for p in raw if len(p) >= 2]
                    if xs and ys:
                        return Box(x0=min(xs), y0=min(ys), x1=max(xs), y1=max(ys))
                elif len(raw) == 4:
                    x0, y0, x1, y1 = (float(v) for v in raw)
                    if x1 > x0 and y1 > y0:
                        return Box(x0=x0, y0=y0, x1=x1, y1=y1)
        except (TypeError, ValueError, IndexError):
            return None
        return None
