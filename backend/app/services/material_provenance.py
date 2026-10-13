"""Derive conservative material provenance from immutable content headings.

Body mentions (for example a DGC instrument in a paragraph) never change the
material role. Only title-like blocks can establish a document or attachment
role; callers bind the returned receipt to their candidate/revision.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

REPORT_TITLE = re.compile(r"(?:瓦斯抽采达标评判报告|瓦斯抽采达标评价报告|瓦斯达标评判报告|评价报告)")
DGC_TITLE = re.compile(r"(?:DGC|可解吸瓦斯量|瓦斯含量).{0,12}(?:检测单|测定单|原始记录|检验报告)", re.I)
DESIGN_TITLE = re.compile(r"(?:设计说明|设计文件|设计报告|施工设计|方案设计)")


@dataclass(frozen=True)
class MaterialProvenance:
    role: str
    basis: str
    quote: str
    page_number: int
    block_id: str | None
    revision_id: str

    def as_dict(self) -> dict[str, Any]:
        return {"role": self.role, "basis": self.basis, "quote": self.quote,
                "page_number": self.page_number, "block_id": self.block_id,
                "content_revision": self.revision_id}


def _title_blocks(page: dict) -> list[dict]:
    blocks = []
    for block in page.get("text_blocks") or []:
        label = str(block.get("label") or "").lower()
        text = str(block.get("text") or block.get("raw_text") or "").strip()
        if not text or len(text) > 180:
            continue
        if label in {"title", "doc_title", "paragraph_title", "heading", "figure_title"}:
            blocks.append(block)
    # Historical artifacts can have no labels; only the first short block on the
    # first two pages is considered a title fallback, never arbitrary body text.
    if not blocks and int(page.get("page_number", 0)) <= 2:
        for block in (page.get("text_blocks") or [])[:2]:
            if block.get("label"):
                continue
            text = str(block.get("text") or block.get("raw_text") or "").strip()
            if text and len(text) <= 180:
                blocks.append(block)
    return blocks


def build_material_provenance(pages: list[dict], revision_id: str) -> dict[int, dict]:
    """Return page-scoped material provenance; source pages remain immutable."""
    headings = []
    for page in sorted(pages, key=lambda item: int(item.get("page_number", 0))):
        number = int(page.get("page_number", 0))
        for block in _title_blocks(page):
            quote = str(block.get("text") or block.get("raw_text") or "").strip()
            role = ("report_body" if REPORT_TITLE.search(quote) else
                    "report_sheet" if DGC_TITLE.search(quote) else
                    "design" if DESIGN_TITLE.search(quote) else None)
            if role:
                headings.append((number, role, quote, block.get("block_id"), block.get("bbox"),
                                 block.get("coordinate_system")))
    report = next((item for item in headings if item[1] == "report_body"), None)
    result: dict[int, dict] = {}
    if report:
        title = MaterialProvenance("report_body", "source.material_title:" + report[2],
                                   report[2], report[0], report[3], str(revision_id))
        for page in pages:
            number = int(page.get("page_number", 0))
            if number < report[0]:
                continue
            result[number] = title.as_dict() | {"scope_page_number": number,
                                                "scope": "document_title", "bbox": report[4],
                                                "coordinate_system": report[5]}
    # An explicit attachment title starts a material range until a subsequent
    # material heading. Body mentions create no heading and cannot switch roles.
    for number in sorted(int(page.get("page_number", 0)) for page in pages):
        previous = [item for item in headings if item[0] <= number]
        if not previous:
            continue
        heading = previous[-1]
        _, role, quote, block_id, bbox, coordinate_system = heading
        if role != "report_body":
            result[number] = MaterialProvenance(role, "source.material_title:" + quote,
                                                 quote, heading[0], block_id, str(revision_id)).as_dict() | {
                "scope_page_number": number, "scope": "attachment_heading",
                "bbox": bbox, "coordinate_system": coordinate_system}
    return result


def provenance_for_page(pages: list[dict], page_number: int, revision_id: str) -> dict | None:
    return build_material_provenance(pages, revision_id).get(int(page_number))
