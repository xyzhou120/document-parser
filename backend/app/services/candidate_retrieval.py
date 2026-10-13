"""Deterministic candidate retrieval for the checklist agent (plan C2, 2026-10-10).

No model is called here.  For each field this module finds short text windows that contain a
configured label and, for numeric fields, at least one number.  The output is a list of
*candidates* for verification; it never produces values.  Each candidate keeps its page, the
text window (at most ``window_chars``), its source (native text layer or OCR), a bounding box
when one is known, and the hints the verifier needs (threshold context, missing unit, table).

Labels come from the rule (``field_name_cn``) and its configured synonyms.  Matching ignores
whitespace and full/half-width differences, and reads OCR markup such as ``m^{3}`` as ``m³``.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

NUMBER_RE = re.compile(r"(?<![A-Za-z\d.])[-+]?\d+(?:\.\d+)?(?!\d)")
# Thresholds, limits and criteria are not measured values (plan C2 exclusion patterns).
THRESHOLD_RE = re.compile(
    r"(不得超过|不超过|不低于|不小于|不大于|不应|应不|临界|限值|标准值|指标值|判定值|规定值|≥|≤|＜|＞|<|>)"
)
COMMON_UNITS = ("m³/min", "m³/t", "m³", "MPa", "kPa", "Pa", "%", "mL", "ml", "℃", "个", "孔", "m", "t")
NUMERIC_TYPES = {"float", "number", "integer", "int", "decimal"}
RANK = {"strong": 0, "partial": 1, "label": 2, "threshold": 3}


def is_ocr_source(source: Any) -> bool:
    """Canonicalize producer source labels without changing persisted provenance."""
    value = str(source or "").strip().lower()
    return value == "ocr" or value == "automatic_ocr" or value.startswith("ocr_")


def normalize(text: Any) -> str:
    """Whitespace-free, half-width form used for matching (labels, numbers, units)."""
    if text is None:
        return ""
    out = []
    for ch in str(text):
        code = ord(ch)
        if 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        elif ch in "　 ":
            out.append(" ")
        else:
            out.append(ch)
    s = "".join(out).replace("$", "")
    s = re.sub(r"\^\{?3\}?", "³", s)
    s = re.sub(r"m3(?![A-Za-z\d])", "m³", s)
    s = re.sub(r"\s+", "", s)
    return s


def normalize_unit(unit: Any) -> str:
    return normalize(unit).lower()


def find_numbers(normalized: str) -> list[str]:
    return NUMBER_RE.findall(normalized)


VALUE_LEAD_CHARS = "：:为共计数量"
VALUE_UNIT_RE = re.compile(r"^(?:个|孔|m³|m3|MPa|kPa|%|m|t|mL|ml|℃)")


def has_value_like_number(normalized: str) -> bool:
    """A number that reads as a measured value: led by a colon or count word, or followed by a unit.

    Enumerators ("1、") and codes ("水仓5-2") are not values, so they do not stop the look-ahead.
    """
    for match in NUMBER_RE.finditer(normalized):
        before = normalized[max(0, match.start() - 4):match.start()]
        after = normalized[match.end():match.end() + 6]
        if any(ch in VALUE_LEAD_CHARS for ch in before) or VALUE_UNIT_RE.match(after):
            return True
    return False


def find_units(normalized: str, extra: tuple[str, ...] = ()) -> list[str]:
    """Units present in the text, longest first.

    Short units must stand alone (``t`` is not inside a word), and a unit already covered by a
    longer match is not reported again (``m³`` and ``t`` inside ``m³/t``).
    """
    found: list[str] = []
    kept_spans: list[tuple[int, int]] = []
    lowered = normalized.lower()
    for unit in sorted({*COMMON_UNITS, *extra}, key=len, reverse=True):
        token = normalize_unit(unit)
        if not token:
            continue
        pattern = (r"(?<![a-z])" + re.escape(token) + r"(?![a-z])"
                   if token[-1].isalpha() else re.escape(token))
        for match in re.finditer(pattern, lowered):
            span = match.span()
            if any(start <= span[0] and span[1] <= end for start, end in kept_spans):
                continue
            kept_spans.append(span)
            if unit not in found:
                found.append(unit)
    return found


def value_in_text(value: Any, text: str, numeric: bool) -> bool:
    """A verified value must literally appear in the candidate text (plan C4)."""
    if value is None:
        return False
    raw = normalize(value)
    if not raw:
        return False
    normalized_text = normalize(text)
    if numeric:
        try:
            wanted = Decimal(raw.replace(",", ""))
        except (InvalidOperation, ValueError):
            return False
        for token in find_numbers(normalized_text):
            try:
                if Decimal(token) == wanted:
                    return True
            except (InvalidOperation, ValueError):
                continue
        return False
    return raw in normalized_text


@dataclass
class FieldSpec:
    field_key: str
    name: str
    labels: list[str]
    unit: str | None
    numeric: bool
    multi: bool
    extra_threshold_terms: tuple[str, ...] = ()
    extra_units: tuple[str, ...] = ()

    @classmethod
    def from_rule(cls, rule: Any) -> FieldSpec:
        knowledge = (getattr(rule, "strategy", None) or {}).get("agent_extract") or {}
        retrieval = knowledge.get("retrieval") or {}
        names = [getattr(rule, "field_name_cn", "") or ""]
        synonyms = knowledge.get("synonyms") or []
        if isinstance(synonyms, str):
            synonyms = [synonyms]
        labels: list[str] = []
        for item in [*names, *[s for s in synonyms if isinstance(s, str)]]:
            label = normalize(item)
            if len(label) >= 2 and label not in labels:
                labels.append(label)
        extra_labels = retrieval.get("labels") or []
        for item in extra_labels if isinstance(extra_labels, list) else []:
            label = normalize(item)
            if len(label) >= 2 and label not in labels:
                labels.append(label)
        data_type = str(getattr(rule, "data_type", "") or "string").lower()
        return cls(
            field_key=rule.field_key,
            name=getattr(rule, "field_name_cn", "") or rule.field_key,
            labels=labels,
            unit=getattr(rule, "unit", None) or None,
            numeric=data_type in NUMERIC_TYPES,
            multi=bool(getattr(rule, "multi_instance", False)),
            extra_threshold_terms=tuple(
                str(t) for t in (retrieval.get("exclude_terms") or []) if isinstance(t, str)),
            extra_units=tuple(
                str(u) for u in (retrieval.get("units") or []) if isinstance(u, str)),
        )


@dataclass
class Segment:
    page_number: int
    index: int
    text: str
    bbox: list[float] | None
    source: str  # "native" | "ocr"
    table: bool = False
    segment_id: str = ""
    table_id: str = ""
    context_hint: str = ""
    original_source: str = ""


@dataclass
class Candidate:
    field_key: str
    page_number: int
    text: str
    labels: list[str]
    numbers: list[str]
    units: list[str]
    source: str
    bbox: list[float] | None
    table: bool
    strength: str
    hints: list[str] = field(default_factory=list)
    segment_ids: list[str] = field(default_factory=list)
    # Nearby lines (the two lines above and the page heading): lets the verifier bind dimensions
    # such as workface or evaluation unit that the value line itself does not repeat.
    context: str = ""
    # Stable across retries and pagination for one immutable content revision.  ``retrieve``
    # keeps this empty for legacy callers that do not provide a revision/hash.
    stable_key: str = ""
    original_source: str = ""

    def as_prompt(self, cid: str) -> dict[str, Any]:
        item: dict[str, Any] = {
            "id": cid,
            "page": self.page_number,
            "source": self.source,
            "table": self.table,
            "text": self.text,
            "numbers": self.numbers,
            "units": self.units,
            "hints": self.hints,
        }
        if self.stable_key:
            item["stable_key"] = self.stable_key
        if self.original_source:
            item["original_source"] = self.original_source
        if self.context:
            item["context"] = self.context
        return item


@dataclass
class RetrievalResult:
    field_key: str
    candidates: list[Candidate]
    total_matches: int
    unreadable_pages: list[int]
    truncated: bool
    # Strength of the matches cut by the cap.  Only strong or partial cuts make a field unfinished.
    dropped: dict[str, int] = field(default_factory=dict)


def stable_candidate_key(
    content_revision: str,
    rule_semantic_hash: str,
    candidate: Candidate,
    strategy_version: str = "retrieval-v2",
) -> str:
    """Return the durable identity used by the candidate ledger.

    The key deliberately uses source coordinates and normalized window text rather than the
    candidate's rank.  Adding a page before an existing match therefore cannot change its
    identity, and rescanning the same range is idempotent.
    """
    payload = {
        "revision": str(content_revision),
        "rule": str(rule_semantic_hash),
        "strategy": str(strategy_version),
        "segments": list(candidate.segment_ids),
        "source": candidate.source,
        "page": int(candidate.page_number),
        "bbox": candidate.bbox,
        "window": normalize(candidate.text),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


# Alias used by integrations that call this a candidate id rather than a key.
candidate_stable_key = stable_candidate_key


def page_readable(page: dict) -> bool:
    """A page whose text could not be read cannot prove that a value is absent (plan C1/C2).

    ``ocr_status`` is set by parse_service: ``not_needed`` (native text is enough), ``ok``
    (OCR text stored), ``failed`` (OCR error or empty answer on a page that needs it).  Pages
    from older artifacts carry no status: an image-bearing page with almost no text is treated
    as unreadable, so a missing OCR layer can never turn into a silent "not found".
    """
    status = page.get("ocr_status")
    if status == "failed":
        return False
    if status is None:
        text = str(page.get("text") or "").strip()
        if int(page.get("image_count") or 0) > 0 and len(text) < 30:
            return False
    return True


def _table_regions(page: dict) -> list[list[float]]:
    regions = []
    for table in page.get("tables") or []:
        bbox = table.get("bbox") if isinstance(table, dict) else None
        if isinstance(bbox, list) and len(bbox) == 4:
            regions.append([float(v) for v in bbox])
    return regions


def _inside(bbox: list[float] | None, regions: list[list[float]]) -> bool:
    if not bbox:
        return False
    cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
    return any(r[0] <= cx <= r[2] and r[1] <= cy <= r[3] for r in regions)


def _union(boxes: list[list[float]]) -> list[float] | None:
    boxes = [b for b in boxes if b]
    if not boxes:
        return None
    return [min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes)]


def _native_lines(blocks: list[dict], page_number: int) -> list[Segment]:
    spans = [
        b for b in blocks
        if not is_ocr_source(b.get("source")) and b.get("bbox") and str(b.get("text") or "").strip()
    ]
    # Adapted/legacy artifacts may carry valid native text blocks without geometry. Keep them
    # as page-level segments instead of silently making the field appear absent; no synthetic
    # bbox is created and the real adapter block identity is retained for evidence linkage.
    loose_blocks = [
        b for b in blocks
        if not is_ocr_source(b.get("source")) and not b.get("bbox") and str(b.get("text") or "").strip()
    ]
    spans.sort(key=lambda b: (round(b["bbox"][1]), b["bbox"][0]))
    lines: list[dict] = []
    for span in spans:
        x0, y0, x1, y1 = (float(v) for v in span["bbox"])
        yc, h = (y0 + y1) / 2, max(1.0, y1 - y0)
        source = "human_correction" if str(span.get("source") or "").lower() == "human_correction" else "native"
        if lines and lines[-1]["source"] == source and abs(yc - lines[-1]["yc"]) <= 0.5 * max(h, lines[-1]["h"]):
            lines[-1]["spans"].append((x0, y0, x1, y1, str(span["text"]), span.get("block_id") or span.get("segment_id")))
        else:
            lines.append({"yc": yc, "h": h, "source": source,
                "original_source": str(span.get("original_source") or span.get("source") or "native"),
                "spans": [(x0, y0, x1, y1, str(span["text"]), span.get("block_id") or span.get("segment_id"))]})
    segments = []
    for line in lines:
        parts = sorted(line["spans"], key=lambda s: s[0])
        text = ""
        prev_x1 = None
        for x0, _y0, x1, _y1, value, _block_id in parts:
            if prev_x1 is not None and x0 - prev_x1 > 2.0:
                text += " "
            text += value
            prev_x1 = x1
        boxes = [[s[0], s[1], s[2], s[3]] for s in parts]
        source_ids = [str(s[5]) for s in parts if s[5]]
        segments.append(Segment(
            page_number=page_number, index=len(segments), text=text.strip(),
            bbox=_union(boxes), source=line["source"], original_source=line["original_source"],
            segment_id=("block:" + ":".join(source_ids) if source_ids else "")))
    for block in loose_blocks:
        block_id = block.get("block_id") or block.get("segment_id")
        segments.append(Segment(
            page_number=page_number,
            index=len(segments),
            text=str(block.get("text") or "").strip(),
            bbox=None,
            source="human_correction" if block.get("source") == "human_correction" else "native",
            original_source=str(block.get("original_source") or block.get("source") or "native"),
            segment_id=(f"block:{block_id}" if block_id else ""),
        ))
    return segments


def _ocr_segments(blocks: list[dict], page_number: int) -> list[Segment]:
    segments: list[Segment] = []
    for block in blocks:
        if not is_ocr_source(block.get("source")):
            continue
        bbox = block.get("bbox")
        bbox = [float(v) for v in bbox] if isinstance(bbox, list) and len(bbox) == 4 else None
        is_table = str(block.get("label") or "") == "table"
        for line_index, line in enumerate(str(block.get("text") or "").splitlines()):
            if line.strip():
                block_id = block.get("block_id") or block.get("segment_id")
                segments.append(Segment(
                    page_number=page_number, index=len(segments), text=line.strip(),
                    bbox=bbox, source="ocr", table=is_table,
                    original_source=str(block.get("original_source") or block.get("source") or "ocr"),
                    segment_id=(f"block:{block_id}:{line_index}" if block_id else "")))
    return segments


def build_segments(page: dict) -> list[Segment]:
    """Reading-order text segments for one page (native lines first, then OCR regions)."""
    page_number = int(page.get("page_number", page.get("page", 0)))
    blocks = page.get("text_blocks") or []
    if blocks:
        segments = _native_lines(blocks, page_number) + _ocr_segments(blocks, page_number)
    else:
        # Pages without geometry (tests, legacy artifacts): one segment per text line.
        segments = [
            Segment(page_number=page_number, index=i, text=line.strip(), bbox=None, source="native")
            for i, line in enumerate(str(page.get("text") or "").splitlines()) if line.strip()
        ]
    regions = _table_regions(page)
    table_metadata = [t for t in (page.get("tables") or []) if isinstance(t, dict)]
    table_ids = [str(t.get("table_id") or t.get("id") or "") for t in table_metadata]
    table_context = " | ".join(
        str(t.get("header") or t.get("continuation_header") or "").strip()
        for t in table_metadata if t.get("header") or t.get("continuation_header")
    )[:240]
    for segment in segments:
        if segment.source == "native" and (_inside(segment.bbox, regions) or (table_metadata and not regions)):
            segment.table = True
        if segment.table or (segment.source == "ocr" and table_metadata):
            segment.table_id = next((tid for tid in table_ids if tid), "")
            segment.context_hint = table_context
    for segment in segments:
        if not segment.segment_id:
            segment.segment_id = f"p{page_number}-s{segment.index}-{segment.source}"
    return segments


def _window(text: str, anchor: int, size: int) -> str:
    if len(text) <= size:
        return text
    start = max(0, min(anchor - size // 5, len(text) - size))
    return text[start:start + size]


def _threshold_context(normalized: str, extra: tuple[str, ...]) -> bool:
    """True only when every number in the window sits next to a threshold marker."""
    pattern = THRESHOLD_RE
    if extra:
        pattern = re.compile("|".join([THRESHOLD_RE.pattern] + [re.escape(t) for t in extra if t]))
    spans = [m.span() for m in NUMBER_RE.finditer(normalized)]
    if not spans:
        return False
    for start, end in spans:
        near = normalized[max(0, start - 8):end + 4]
        if not pattern.search(near):
            return False
    return True


def _context_for(segments: list[Segment], index: int, window_segments: list[Segment]) -> str:
    """Two preceding lines of the same page and source, plus the page heading when it is not already
    among them.  Capped so that context never dominates the candidate window."""
    seg = segments[index]
    before: list[Segment] = []
    j = index - 1
    while j >= 0 and len(before) < 2 and segments[j].source == seg.source \
            and (segments[j].page_number == seg.page_number
                 or (seg.table_id and segments[j].table_id == seg.table_id)):
        before.insert(0, segments[j])
        j -= 1
    parts: list[str] = []
    heading = next((s for s in segments if s.page_number == seg.page_number and s.source == seg.source), None)
    if heading is not None and heading is not seg and heading not in before and heading not in window_segments:
        parts.append(heading.text)
    if seg.context_hint:
        parts.append(seg.context_hint)
    parts.extend(s.text for s in before)
    return " | ".join(parts)[:240]


def retrieve(field_spec: FieldSpec, pages: list[dict], *, max_candidates: int | None,
             window_chars: int = 300, content_revision: str = "",
             rule_semantic_hash: str = "", strategy_version: str = "retrieval-v2") -> RetrievalResult:
    """Return the highest-priority candidate windows for one field.

    Ranking: strong (label, number, expected unit) before partial (label and number, unit
    missing or different), then label-only windows for non-numeric fields, then threshold
    context.  Ties keep reading order, so earlier pages are preferred for the same strength.
    """
    ordered_pages = sorted(pages, key=lambda p: int(p.get("page_number", p.get("page", 0))))
    unreadable = [int(p.get("page_number", p.get("page", 0))) for p in ordered_pages
                  if not page_readable(p)]
    segments: list[Segment] = []
    for page in ordered_pages:
        segments.extend(build_segments(page))
    expected_unit = normalize_unit(field_spec.unit) if field_spec.unit else None
    matches: list[tuple[tuple, Candidate]] = []
    seen_windows: set[tuple[int, str]] = set()
    for i, seg in enumerate(segments):
        head = normalize(seg.text)
        nxt = segments[i + 1] if i + 1 < len(segments) else None
        joined_next = (nxt is not None and nxt.page_number == seg.page_number
                       and nxt.source == seg.source)
        tail = normalize(nxt.text) if joined_next else ""
        labels_hit = []
        anchor = None
        for label_index, label in enumerate(field_spec.labels):
            pos = head.find(label)
            if pos >= 0:
                labels_hit.append((label_index, label))
                anchor = pos if anchor is None else min(anchor, pos)
        if not labels_hit and tail:
            # A label may wrap across two lines or cells: it must start in this segment.
            combined = head + tail
            for label_index, label in enumerate(field_spec.labels):
                pos = combined.find(label)
                if 0 <= pos < len(head):
                    labels_hit.append((label_index, label))
                    anchor = pos if anchor is None else min(anchor, pos)
        if not labels_hit:
            continue
        window_segments = [seg]
        if field_spec.numeric and not has_value_like_number(head):
            # List-style reports put the value one or two lines below its label ("（2）数量：75 个"),
            # often after a sub-item such as a diameter.  Take up to two more lines of the same page
            # and source; the verifier decides which number, if any, is the target.
            following = i + 1
            while len(window_segments) < 3 and following < len(segments):
                candidate_line = segments[following]
                if candidate_line.page_number != seg.page_number or candidate_line.source != seg.source:
                    break
                window_segments.append(candidate_line)
                following += 1
        window_text = " ".join(s.text for s in window_segments).strip()
        normalized = normalize(window_text)
        numbers = find_numbers(normalized)
        if field_spec.numeric and not numbers:
            continue  # label without a value nearby: weak, not a candidate
        key = (seg.page_number, normalize(window_text))
        if key in seen_windows:
            continue
        seen_windows.add(key)
        units = find_units(normalized, field_spec.extra_units)
        hints: list[str] = []
        if expected_unit is None:
            unit_ok = True
        else:
            unit_ok = expected_unit in [normalize_unit(u) for u in units]
            if not unit_ok:
                hints.append("unit_missing" if not units else "unit_mismatch")
        threshold = bool(numbers) and _threshold_context(normalized,
                                                       field_spec.extra_threshold_terms)
        if threshold:
            strength = "threshold"
            hints.append("threshold_context")
        elif not field_spec.numeric:
            strength = "label"
        elif unit_ok:
            strength = "strong"
        else:
            strength = "partial"
        if len(window_segments) > 1:
            hints.append("label_or_value_wrapped")
        if seg.source == "ocr":
            hints.append("ocr")
        if seg.table or any(s.table for s in window_segments):
            hints.append("table")
        if len(window_text) > window_chars:
            window_text = _window(window_text, anchor or 0, window_chars)
        boxes = _union([s.bbox for s in window_segments if s.bbox])
        context = _context_for(segments, i, window_segments)
        candidate = Candidate(
            field_key=field_spec.field_key,
            page_number=seg.page_number,
            text=window_text,
            labels=[label for _, label in labels_hit],
            numbers=numbers,
            units=units,
            source=seg.source,
            bbox=boxes,
            table=seg.table or any(s.table for s in window_segments),
            strength=strength,
            hints=hints,
            segment_ids=[s.segment_id for s in window_segments],
            context=context,
            original_source=seg.original_source,
        )
        if content_revision or rule_semantic_hash:
            candidate.stable_key = stable_candidate_key(
                content_revision, rule_semantic_hash, candidate, strategy_version)
        order = (RANK[strength], min(idx for idx, _ in labels_hit), -len(labels_hit),
                 seg.page_number, i)
        matches.append((order, candidate))
    matches.sort(key=lambda item: item[0])
    chosen = [c for _, c in (matches if max_candidates is None else matches[:max_candidates])]
    dropped: dict[str, int] = {}
    for _, candidate in (() if max_candidates is None else matches[max_candidates:]):
        dropped[candidate.strength] = dropped.get(candidate.strength, 0) + 1
    return RetrievalResult(
        field_key=field_spec.field_key,
        candidates=chosen,
        total_matches=len(matches),
        unreadable_pages=unreadable,
        truncated=max_candidates is not None and len(matches) > max_candidates,
        dropped=dropped,
    )
