"""
PDF解析器
提取PDF文本、页面图片、表格结构，检测页面类型
"""

import logging
from typing import Dict, List

import pymupdf  # PyMuPDF

from app.models.contracts import PageInspection

logger = logging.getLogger(__name__)
DEFAULT_RENDER_DPI = 150
DEFAULT_MAX_TABLE_CELLS = 2000


class PDFParser:
    """PDF解析器"""

    def __init__(self):
        self.dpi = DEFAULT_RENDER_DPI
        self.max_table_cells = DEFAULT_MAX_TABLE_CELLS

    def extract_text(self, pdf_path: str) -> str:
        """
        提取整个PDF的文本

        Args:
            pdf_path: PDF文件路径

        Returns:
            全文文本
        """
        try:
            doc = pymupdf.open(pdf_path)
            text_parts = []
            for page in doc:
                text_parts.append(page.get_text())
            doc.close()
            return "\n\n".join(text_parts)
        except Exception as e:
            logger.error(f"Failed to extract text from {pdf_path}: {e}")
            raise

    def extract_pages(self, pdf_path: str, with_tables: bool = True) -> List[Dict]:
        """
        逐页提取文本、表格和元数据

        Args:
            pdf_path: PDF文件路径
            with_tables: 是否识别表格结构

        Returns:
            页面列表，每页包含：page_number, text, tables, char_count, image_count, width, height
        """
        try:
            doc = pymupdf.open(pdf_path)
            pages = []

            for page_num, page in enumerate(doc, start=1):
                text = page.get_text()
                text_blocks = []
                offset = 0
                for block in page.get_text("dict").get("blocks", []):
                    for line in block.get("lines", []):
                        for span in line.get("spans", []):
                            start = text.find(span["text"], offset)
                            end = start + len(span["text"]) if start >= 0 else None
                            text_blocks.append({
                                "text": span["text"], "bbox": list(span["bbox"]),
                                "coordinate_system": "PDF_POINT", "source": "native",
                                "coordinate_origin": "MUPDF_UNROTATED_TOP_LEFT",
                                "page_number": page_num,
                                "start_offset": start if start >= 0 else None,
                                "end_offset": end,
                            })
                            if end is not None:
                                offset = end
                images = page.get_images()
                tables = self._extract_tables(page) if with_tables else []

                pages.append(
                    {
                        "page_number": page_num,
                        "text": text,
                        "native_text": text,
                        "text_blocks": text_blocks,
                        "tables": tables,
                        "char_count": len(text.strip()),
                        "image_count": len(images),
                        "width": page.rect.width,
                        "height": page.rect.height,
                        "rotation": page.rotation,
                        "cropbox": list(page.cropbox),
                        "mediabox": list(page.mediabox),
                        "unrotated_width": page.cropbox.width,
                        "unrotated_height": page.cropbox.height,
                    }
                )

            doc.close()
            logger.info(f"Extracted {len(pages)} pages from {pdf_path}")
            return pages
        except Exception as e:
            logger.error(f"Failed to extract pages from {pdf_path}: {e}")
            raise

    def _extract_tables(self, page) -> List[Dict]:
        """
        识别页面中的表格结构

        使用 PyMuPDF 内置表格识别。识别失败或该页无表格时返回空列表，
        调用方需容忍空结果并退化为文本匹配。
        """
        tables: List[Dict] = []
        try:
            finder = page.find_tables()
        except Exception as e:
            logger.debug(f"Table detection unavailable on page {page.number + 1}: {e}")
            return tables

        for table in getattr(finder, "tables", []) or []:
            try:
                rows = table.extract()
            except Exception as e:
                logger.debug(f"Failed to extract table rows: {e}")
                continue
            if not rows:
                continue
            cell_count = sum(len(row or []) for row in rows)
            if cell_count > self.max_table_cells:
                logger.warning(
                    f"Skipping oversized table on page {page.number + 1} ({cell_count} cells)"
                )
                continue
            bbox = list(getattr(table, "bbox", []) or [])
            title = ""
            if len(bbox) == 4:
                # 只使用该表上方附近文本定位标题，避免同页其它表标题误匹配。
                title = page.get_text(clip=pymupdf.Rect(0, max(0, bbox[1] - 60), page.rect.width, bbox[1]))
            tables.append(
                {
                    "rows": rows,
                    "row_count": len(rows),
                    "col_count": max((len(row or []) for row in rows), default=0),
                    "bbox": bbox,
                    "title": title,
                    "table_id": f"p{page.number + 1}-t{len(tables) + 1}",
                    "header": {
                        "names": list(getattr(getattr(table, "header", None), "names", []) or []),
                        "external": bool(getattr(getattr(table, "header", None), "external", False)),
                        "bbox": list(getattr(getattr(table, "header", None), "bbox", []) or []),
                        "semantic_status": "UNVERIFIED",
                    },
                    "cells": [
                        {"row": r, "col": c, "row_index": r, "column_index": c, "text": rows[r][c],
                         "cell_id": f"p{page.number + 1}-t{len(tables) + 1}-r{r}-c{c}",
                         "bbox": list(cell) if cell else None,
                         "coordinate_system": "PDF_POINT", "coordinate_origin": "MUPDF_UNROTATED_TOP_LEFT"}
                        for r, row in enumerate(getattr(table, "rows", []))
                        for c, cell in enumerate(row.cells)
                        if r < len(rows) and c < len(rows[r])
                    ],
                }
            )
        return tables

    def get_page_image(self, pdf_path: str, page_number: int) -> bytes:
        """
        获取指定页面的图片（PNG格式）

        Args:
            pdf_path: PDF文件路径
            page_number: 页码（从1开始）

        Returns:
            PNG图片字节
        """
        try:
            doc = pymupdf.open(pdf_path)
            page = doc[page_number - 1]

            # 转换为图片
            pix = page.get_pixmap(dpi=self.dpi)
            img_bytes = pix.tobytes("png")

            doc.close()
            return img_bytes
        except Exception as e:
            logger.error(f"Failed to get page image from {pdf_path} page {page_number}: {e}")
            raise

    def inspect_pages(self, pdf_path: str) -> List[PageInspection]:
        """
        检测哪些页面需要OCR

        逻辑：
        - char_count < 100 且有图片 → 可能是扫描件，需要OCR
        - image_count >= 页面大部分 → 可能是扫描件
        - char_count 很少 → 需要OCR

        Args:
            pdf_path: PDF文件路径

        Returns:
            页面检测结果列表
        """
        # 表格结构与 OCR 判断无关，这里跳过以节省时间
        pages = self.extract_pages(pdf_path, with_tables=False)
        inspections = []

        for page in pages:
            char_count = page["char_count"]
            image_count = page["image_count"]

            # 判断是否需要OCR
            is_text_heavy = char_count > 500
            is_scan = char_count < 100 and image_count > 0
            # 文本较少但没有页面图像时仍可能是正常的短页；只有扫描/图像页才调用 OCR。
            needs_ocr = is_scan or (char_count < 200 and image_count > 0)

            inspections.append(
                PageInspection(
                    page_number=page["page_number"],
                    char_count=char_count,
                    image_count=image_count,
                    needs_ocr=needs_ocr,
                    is_text_heavy=is_text_heavy,
                    is_scan=is_scan,
                )
            )

        logger.info(
            f"Inspected {len(inspections)} pages, "
            f"{sum(1 for i in inspections if i.needs_ocr)} need OCR"
        )
        return inspections

    def get_page_count(self, pdf_path: str) -> int:
        """
        获取PDF页数

        Args:
            pdf_path: PDF文件路径

        Returns:
            页数
        """
        try:
            doc = pymupdf.open(pdf_path)
            page_count = doc.page_count
            doc.close()
            return page_count
        except Exception as e:
            logger.error(f"Failed to get page count from {pdf_path}: {e}")
            raise
