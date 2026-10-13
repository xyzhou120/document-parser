"""将不可变 DocumentArtifact 页面适配为内容契约。

产物始终是文档文本的唯一来源。本模块有意不写数据库，因此提取、索引和助手
工作进程可用相同方式使用。内容适配器必须具有确定性：对同一产物运行两次，
应产生相同的块、表格、区域标识和内容摘要。
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

ADAPTER_VERSION = "content-adapter-v1"


def _json_digest(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _stable_id(prefix: str, value: Any) -> str:
    return f"{prefix}-{_json_digest(value)[:20]}"


def normalize_text(value: Any) -> str:
    """保留原始文本，同时返回用于比较的形式。"""
    text = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", " ", text).strip()


def _bbox(value: Any) -> list[float] | None:
    if isinstance(value, Mapping):
        value = [value.get(key) for key in ("x0", "y0", "x1", "y1")]
    if not isinstance(value, list | tuple) or len(value) != 4:
        return None
    try:
        return [float(item) for item in value]
    except (TypeError, ValueError):
        return None


def _source_block(page_number: int, block: Mapping[str, Any], ordinal: int | None) -> dict[str, Any]:
    source = str(block.get("source") or "native").lower()
    if source.startswith("ocr"):
        source = "ocr"
    elif source in {"human_correction", "automatic_ocr", "native", "table", "formula"}:
        source = source
    else:
        source = "native"
    raw = str(block.get("raw_text", block.get("text", "")) or "")
    normalized = normalize_text(block.get("normalized_text", block.get("text", raw)))
    locator = deepcopy(block.get("original_locator") or block.get("locator"))
    bbox = _bbox(block.get("bbox"))
    coordinate_system = block.get("coordinate_system")
    if bbox is None and isinstance(locator, Mapping):
        bbox = _bbox(locator.get("bbox"))
        coordinate_system = locator.get("coordinate_system")
    identity = {
        "page": page_number,
        "source": source,
        "text": raw,
        "normalized": normalized,
        "bbox": bbox,
        "locator": locator,
        # 现有生产者可能提供显式身份。否则序号只用于区分重复的来源范围；
        # 有意排除偏移量，因为修正可能移动后续所有偏移。
        "explicit_id": block.get("block_id") or block.get("segment_id"),
        "duplicate_ordinal": ordinal,
    }
    return {
        **deepcopy(dict(block)),
        "block_id": str(block.get("block_id") or block.get("segment_id") or _stable_id("blk", identity)),
        "region_id": str(block.get("region_id") or _stable_id("reg", identity)) if source in {"ocr", "automatic_ocr"} else None,
        "page_number": page_number,
        "physical_page": page_number,
        "source": source,
        "original_source": block.get("source"),
        "raw_text": raw,
        "text": str(block.get("text", raw) or ""),
        "normalized_text": normalized,
        "bbox": bbox,
        "coordinate_system": coordinate_system,
        "coordinate_origin": block.get("coordinate_origin"),
        "locator": locator,
        "start_offset": block.get("start_offset"),
        "end_offset": block.get("end_offset"),
        "confidence": block.get("confidence"),
        "label": block.get("label"),
    }


def _adapt_table(page_number: int, table: Mapping[str, Any], ordinal: int) -> dict[str, Any]:
    raw_id = table.get("table_id")
    identity = {
        "page": page_number,
        "ordinal": ordinal,
        "table_id": raw_id,
        "bbox": _bbox(table.get("bbox")),
        "rows": table.get("rows") or [],
        "title": table.get("title"),
    }
    table_id = str(raw_id) if raw_id else _stable_id("tbl", identity)
    cells: list[dict[str, Any]] = []
    for cell_ordinal, cell in enumerate(table.get("cells") or []):
        cell = dict(cell)
        row = cell.get("row", cell.get("row_index"))
        col = cell.get("col", cell.get("column_index"))
        cell_identity = {"table": table_id, "row": row, "col": col, "text": cell.get("text"), "bbox": _bbox(cell.get("bbox")), "ordinal": cell_ordinal}
        cells.append({
            **deepcopy(cell),
            "cell_id": str(cell.get("cell_id") or _stable_id("cell", cell_identity)),
            "row_index": row,
            "column_index": col,
            "raw_text": str(cell.get("raw_text", cell.get("text", "")) or ""),
            "text": str(cell.get("text") or ""),
            "normalized_text": normalize_text(cell.get("text", "")),
            "bbox": _bbox(cell.get("bbox")),
            "coordinate_system": cell.get("coordinate_system"),
            "coordinate_origin": cell.get("coordinate_origin"),
        })
    header = deepcopy(dict(table.get("header") or {}))
    header.setdefault("header_id", f"{table_id}:header")
    return {
        **deepcopy(dict(table)),
        "table_id": table_id,
        "page_number": page_number,
        "physical_page": page_number,
        "bbox": _bbox(table.get("bbox")),
        "raw_title": str(table.get("title") or ""),
        "title": str(table.get("title") or ""),
        "normalized_title": normalize_text(table.get("title")),
        "rows": deepcopy(table.get("rows") or []),
        "row_ids": [f"{table_id}:row:{i}" for i, _ in enumerate(table.get("rows") or [])],
        "row_count": table.get("row_count"),
        "col_count": table.get("col_count"),
        "header": header,
        "cells": cells,
    }


def _page(page: Mapping[str, Any], ordinal: int) -> dict[str, Any]:
    page_number = int(page.get("page_number", page.get("page", ordinal + 1)))
    has_ocr = bool(page.get("ocr_blocks")) or any(
        (str(block.get("source", "")).lower().startswith("ocr") or block.get("source") == "automatic_ocr")
        for block in page.get("text_blocks") or []
    )
    native_raw = str(page.get("native_text", "" if has_ocr else page.get("text", "")) or "")
    ocr_raw = str(page.get("ocr_text", "") or "")
    blocks = []
    raw_blocks = list(page.get("text_blocks") or [])
    block_keys = [
        _json_digest({"source": b.get("source"), "text": b.get("raw_text", b.get("text", "")), "bbox": _bbox(b.get("bbox")), "locator": b.get("original_locator") or b.get("locator")})
        for b in raw_blocks
    ]
    counts: dict[str, int] = {}
    for key in block_keys:
        counts[key] = counts.get(key, 0) + 1
    seen_blocks: dict[str, int] = {}
    for block, raw_key in zip(raw_blocks, block_keys, strict=True):
        occurrence = seen_blocks.get(raw_key, 0)
        seen_blocks[raw_key] = occurrence + 1
        blocks.append(_source_block(page_number, block, occurrence if counts[raw_key] > 1 else None))
    # 一些旧产物只在 ocr_blocks 中保存 OCR。将其暴露为区域，但不修改原始 text_blocks 列表。
    existing_regions = [b for b in blocks if b["source"] in {"ocr", "automatic_ocr"}]
    for block in page.get("ocr_blocks") or []:
        block = dict(block)
        locator = block.get("locator") or block.get("original_locator")
        raw = str(block.get("text", "") or "")
        # parse_service 会保存两份相同 OCR 结果：PDF_POINT 文本块和原始 PIXEL 定位器。
        # 比较原始定位器，避免在不同坐标系中引入第二个出现项。
        duplicate = any(
            b["raw_text"] == raw and (
                (locator is not None and b.get("locator") == locator)
                or (_bbox(block.get("bbox")) is not None and b.get("bbox") == _bbox(block.get("bbox")))
                or (locator is None and not block.get("bbox"))
            )
            for b in existing_regions
        )
        if not duplicate:
            raw_key = _json_digest({"source": "ocr", "text": block.get("text", ""), "bbox": _bbox(block.get("bbox")), "locator": block.get("original_locator") or block.get("locator")})
            occurrence = seen_blocks.get(raw_key, 0)
            seen_blocks[raw_key] = occurrence + 1
            blocks.append(_source_block(page_number, {**block, "source": "ocr"}, occurrence if counts.get(raw_key, 0) > 1 else None))
    if not blocks and page.get("text"):
        # 历史文本产物仍需要可搜索内容，此时使用页面级位置，不伪造几何位置。
        blocks.append(_source_block(page_number, {"text": page["text"], "source": "native", "start_offset": 0, "end_offset": len(page["text"])}, None))
    if not ocr_raw:
        ocr_raw = "\n".join(b["raw_text"] for b in blocks if b["source"] in {"ocr", "automatic_ocr"})
    tables = [_adapt_table(page_number, table, i) for i, table in enumerate(page.get("tables") or [])]
    return {
        **deepcopy(dict(page)),
        "page_number": page_number,
        "physical_page": page_number,
        "text": str(page.get("text", native_raw + ("\n" + ocr_raw if ocr_raw else "")) or ""),
        "normalized_text": normalize_text(page.get("text", native_raw + ("\n" + ocr_raw if ocr_raw else ""))),
        "native_text_raw": native_raw,
        "native_text": native_raw,
        "native_normalized_text": normalize_text(native_raw),
        "ocr_text_raw": ocr_raw,
        "ocr_text": ocr_raw,
        "ocr_normalized_text": normalize_text(ocr_raw),
        "text_blocks": blocks,
        "regions": [b for b in blocks if b["source"] in {"ocr", "automatic_ocr"}],
        "tables": tables,
        "width": page.get("width"),
        "height": page.get("height"),
        "rotation": page.get("rotation"),
        "coverage_status": page.get("coverage_status") or page.get("ocr_status"),
    }


def _coverage(artifact: Any, pages: list[dict[str, Any]]) -> dict[str, Any]:
    quality = dict(getattr(artifact, "quality", None) or {})
    declared = deepcopy(quality.get("coverage_manifest") or {})
    expected = declared.get("expected_page_count") or quality.get("page_count") or len(pages)
    expected = int(expected or 0)
    present = {int(p["page_number"]) for p in pages}
    failed = {int(p) for p in quality.get("failed_pages", []) or []}
    failed.update(int(p) for p in quality.get("source_unreadable_pages", []) or [])
    failed.update(p["page_number"] for p in pages if p.get("source_unreadable") or p.get("ocr_status") == "failed")
    declared_unknown = {int(p) for p in quality.get("unknown_pages", []) or []}
    unknown = (set(range(1, expected + 1)) - present) | declared_unknown
    declared_pages = list(declared.get("pages") or [])
    closed_pages: set[int] = set()
    regions: list[dict[str, Any]] = []
    for item in declared_pages:
        number = int(item["page_number"])
        page_regions = list(item.get("regions") or [])
        closed = bool(page_regions)
        for i, region in enumerate(page_regions):
            region = deepcopy(region)
            state = str(region.get("state", "unknown")).lower()
            region.setdefault("region_id", _stable_id("scope", {"page": number, "bbox": region.get("bbox"), "ordinal": i}))
            region["page_number"] = number
            region["state"] = state
            regions.append(region)
            if state not in {"processed", "blank"}:
                closed = False
            if state in {"unreadable", "failed"}:
                failed.add(number)
        # 生产者必须明确声明区域列表覆盖整个物理页面；裁剪成功本身不代表整页覆盖。
        if closed and item.get("scope_complete") is True:
            closed_pages.add(number)
    expected_pages = set(range(1, expected + 1))
    covered = (closed_pages & present) - failed - declared_unknown
    if declared:
        unknown |= expected_pages - covered - failed
    else:
        # READY、page_count 或存在 OCR 都不足以证明页面已完整可读；历史产物的范围未知。
        unknown |= expected_pages
    unknown -= failed
    is_ready = str(getattr(artifact, "status", "")).upper() == "READY"
    status = "full" if is_ready and expected > 0 and covered == expected_pages and not failed and not unknown else "partial" if declared or failed or not is_ready else "unknown"
    return {
        "status": status,
        "expected_pages": list(range(1, expected + 1)),
        "covered_pages": sorted(covered),
        "failed_pages": sorted(failed),
        "unknown_pages": sorted(unknown),
        "page_count": expected,
        "can_claim_absence": status == "full",
        "regions": regions,
        "artifact_status": str(getattr(artifact, "status", "")),
        "weak_pages": sorted(int(p) for p in quality.get("weak_pages", []) or []),
    }


@dataclass(frozen=True)
class AdaptedContent:
    artifact_id: str
    document_id: str
    file_hash: str
    adapter_version: str
    pages: list[dict[str, Any]]
    coverage_manifest: dict[str, Any]
    content_digest: str
    coverage_digest: str

    @property
    def coverage_complete(self) -> bool:
        return bool(self.coverage_manifest.get("can_claim_absence"))


class ContentAdapter:
    """在不修改产物 JSON 的前提下确定性地暴露内容。"""

    version = ADAPTER_VERSION

    def adapt(self, artifact: Any) -> AdaptedContent:
        raw_pages = list(getattr(artifact, "pages", None) or [])
        pages = [_page(page, i) for i, page in enumerate(raw_pages)]
        pages.sort(key=lambda item: item["page_number"])
        coverage = _coverage(artifact, pages)
        # 摘要包含原始文本和几何信息。即使只发生规范化变更，
        # 下游修订消费者也必须能够感知。
        digest_payload = {"adapter_version": self.version, "pages": pages}
        return AdaptedContent(
            artifact_id=str(getattr(artifact, "artifact_id", "")),
            document_id=str(getattr(artifact, "document_id", "")),
            file_hash=str(getattr(artifact, "file_hash", "")),
            adapter_version=self.version,
            pages=pages,
            coverage_manifest=coverage,
            content_digest=_json_digest(digest_payload),
            coverage_digest=_json_digest(coverage),
        )


content_adapter = ContentAdapter()
