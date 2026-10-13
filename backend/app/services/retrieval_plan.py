"""Durable retrieval planning primitives for P06.

The planner intentionally has no database or model dependency.  A scan cursor describes only
which source range has been read; judgement progress is represented by candidate states in the
candidate ledger and therefore survives worker restarts and changes in batch ordering.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Iterable, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from app.services.candidate_retrieval import (
    Candidate,
    FieldSpec,
    build_segments,
    find_numbers,
    find_units,
    normalize,
    page_readable,
    retrieve,
    stable_candidate_key,
)

ALL_CANDIDATE_STATES = frozenset({"pending", "in_flight", "accepted", "rejected", "duplicate", "unresolved"})
BATCH_STATES = frozenset({"pending", "in_flight", "split", "partially_committed", "done", "failed"})


class CandidateBatchTree:
    """JSON checkpoint for recoverable P07 split trees, independent from scan cursors."""

    def __init__(self, snapshot: dict[str, dict[str, Any]] | None = None):
        self.batches = deepcopy(snapshot or {})

    def as_dict(self) -> dict[str, dict[str, Any]]:
        return deepcopy(self.batches)

    def add(self, batch_id: str, candidate_ids: Sequence[str], *, parent_batch: str | None = None,
            split_path: Sequence[str] = (), payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if not candidate_ids or len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("batch requires a nonempty unique candidate set")
        row = {"batch_id": batch_id, "candidate_ids": list(candidate_ids),
            "parent_batch": parent_batch, "split_path": list(split_path),
            "batch_state": "pending", "decided_ids": [], "children": [],
            "payload": deepcopy(payload or {})}
        row["unit_work_ids"] = list((payload or {}).get("unit_work_ids") or [])
        row["candidate_unit_work_ids"] = deepcopy((payload or {}).get("candidate_unit_work_ids") or {})
        existing = self.batches.get(batch_id)
        if existing:
            if (set(existing["candidate_ids"]) != set(candidate_ids)
                    or existing["parent_batch"] != parent_batch
                    or existing["split_path"] != list(split_path)
                    or existing["payload"] != deepcopy(payload or {})):
                raise ValueError("batch ID reused for different work")
            return existing
        self.batches[batch_id] = row
        return row

    def apply_outcome(self, batch_id: str, outcome: Any) -> list[str]:
        """Save validated decisions and children; never advance a scan cursor.

        Missing/invalid IDs remain pending. Split child payloads retain the complete P07 input,
        including OCR image_refs and object-level units, so restart cannot lose the proof image.
        """
        parent = self.batches[batch_id]
        if parent["batch_state"] == "done":
            return []
        if parent["batch_state"] == "split":
            raise ValueError("late parent outcome cannot overwrite a split batch")
        expected = set(parent["candidate_ids"])
        decisions = getattr(outcome, "decisions", None) or []
        splits = getattr(outcome, "split_batches", None) or []
        if splits:
            # A truncated batch commits no parent decisions.
            pending = expected - set(parent["decided_ids"])
            child_ids = []
            seen: set[str] = set()
            staged = []
            unit_mode = any(split.get("unit_work_ids") for split in splits)
            parent_units = set(parent.get("unit_work_ids") or [])
            if unit_mode:
                declared_sets = [set(split.get("parent_unit_work_ids") or []) for split in splits]
                if not declared_sets[0] or any(values != declared_sets[0] for values in declared_sets):
                    raise ValueError("unit split must declare the same complete parent unit scope")
                if parent_units and parent_units != declared_sets[0]:
                    raise ValueError("unit split changed its parent scope")
                parent_units = declared_sets[0]
            seen_units: set[str] = set()
            for split in splits:
                ids = [str(v) for v in split["candidate_ids"]]
                if not ids or len(ids) != len(set(ids)) or not set(ids) <= pending:
                    raise ValueError("split introduced an unknown candidate")
                if seen.intersection(ids) and not unit_mode:
                    raise ValueError("split child candidate sets overlap")
                seen.update(ids)
                units = set(split.get("unit_work_ids") or [])
                if unit_mode:
                    if not units or seen_units.intersection(units) or not units <= parent_units:
                        raise ValueError("unit work scopes overlap or escape parent")
                    seen_units.update(units)
                    mapping = split.get("candidate_unit_work_ids") or {}
                    mapped = [value for values in mapping.values() for value in values]
                    if (set(mapping) != set(ids) or set(mapped) != units
                            or len(mapped) != len(set(mapped))):
                        raise ValueError("unit work candidate mapping mismatch")
                path = list(split.get("split_path") or [])
                if (not path or path[:len(parent["split_path"])] != parent["split_path"]
                        or len(path) != len(parent["split_path"]) + 1):
                    raise ValueError("split child path must extend its parent")
                child_id = batch_id + "/" + "/".join(path)
                if child_id in child_ids:
                    raise ValueError("split child paths must be unique")
                payload = split.get("payload") if isinstance(split.get("payload"), dict) else split
                payload_ids = payload.get("candidate_ids") if isinstance(payload, dict) else None
                if payload_ids is not None and [str(v) for v in payload_ids] != ids:
                    raise ValueError("split payload candidate IDs mismatch")
                if payload.get("split_path", path) != path:
                    raise ValueError("split payload path mismatch")
                if "candidates" in payload:
                    embedded = [str(c.get("candidate_id") or c.get("id") or "") for c in payload["candidates"]]
                    if embedded != ids:
                        raise ValueError("split candidate payload identity mismatch")
                    if unit_mode:
                        for embedded_candidate in payload["candidates"]:
                            data = embedded_candidate.get("payload") or {}
                            work_ids = data.get("unit_work_ids") or []
                            candidate_id = str(embedded_candidate.get("candidate_id") or embedded_candidate.get("id"))
                            if set(work_ids) != set(mapping[candidate_id]):
                                raise ValueError("embedded candidate unit scope mismatch")
                            if len(embedded_candidate.get("units") or data.get("units") or []) != len(work_ids):
                                raise ValueError("unit payload does not retain every scoped unit")
                staged.append((child_id, ids, path, payload))
                child_ids.append(child_id)
            if seen != pending or (unit_mode and seen_units != parent_units):
                raise ValueError("split must retain every pending candidate")
            # Validate the entire proposal before mutating the checkpoint; malformed children
            # may never leave a partial split tree in memory to be persisted accidentally.
            for child_id, ids, path, payload in staged:
                self.add(child_id, ids, parent_batch=batch_id, split_path=path, payload=payload)
            if unit_mode:
                parent["unit_work_ids"] = sorted(parent_units)
            parent["children"] = child_ids
            parent["batch_state"] = "split"
            return []
        ids = [str(d["candidate_id"]) for d in decisions if isinstance(d, dict)
               and d.get("candidate_id") in expected
               and d.get("judgement") in {"accepted", "rejected", "duplicate"}]
        unique_ids = {value for value in ids if ids.count(value) == 1}
        parent["decided_ids"] = sorted(set(parent["decided_ids"]) | unique_ids)
        if parent.get("unit_work_ids"):
            parent["decided_unit_work_ids"] = sorted({work_id
                for cid in parent["decided_ids"]
                for work_id in parent.get("candidate_unit_work_ids", {}).get(cid, [])})
        remaining = expected - set(parent["decided_ids"])
        parent["batch_state"] = "done" if not remaining else (
            "partially_committed" if parent["decided_ids"] else "pending")
        self._close_ancestors(batch_id)
        return sorted(remaining)

    def _close_ancestors(self, batch_id: str) -> None:
        parent_id = self.batches[batch_id]["parent_batch"]
        if parent_id is None:
            return
        parent = self.batches[parent_id]
        if parent["children"] and all(self.batches[key]["batch_state"] == "done"
                                      for key in parent["children"]):
            parent["batch_state"] = "done"
            parent["decided_ids"] = list(parent["candidate_ids"])
            parent["decided_unit_work_ids"] = list(parent.get("unit_work_ids") or [])
            self._close_ancestors(parent_id)


@dataclass(frozen=True)
class RangeShard:
    """One deterministic strategy x page range work item."""

    strategy_version: str
    range_key: str
    start_page: int
    end_page: int
    page_numbers: tuple[int, ...] = ()
    shard_key: str = field(init=False)

    def __post_init__(self) -> None:
        if self.end_page < self.start_page:
            raise ValueError("end_page must be >= start_page")
        object.__setattr__(self, "shard_key", f"{self.strategy_version}:{self.range_key}")

    def contains(self, page_number: int) -> bool:
        return page_number in self.page_numbers if self.page_numbers else self.start_page <= page_number <= self.end_page


@dataclass
class ScanCursor:
    """Serializable scan position.  It is deliberately independent from judgement cursors."""

    revision: str = ""
    page_index: int = 0
    segment_index: int = 0
    range_key: str = ""
    done: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "page_index": self.page_index,
            "segment_index": self.segment_index,
            "range_key": self.range_key,
            "done": self.done,
            "cursor_kind": "scan",
        }

    @classmethod
    def from_dict(cls, value: Any) -> ScanCursor:
        if not isinstance(value, dict):
            return cls()
        return cls(
            revision=str(value.get("revision") or ""),
            page_index=max(0, int(value.get("page_index") or 0)),
            segment_index=max(0, int(value.get("segment_index") or 0)),
            range_key=str(value.get("range_key") or ""),
            done=bool(value.get("done", False)),
        )


def encode_cursor(cursor: ScanCursor | dict[str, Any]) -> str:
    payload = cursor.as_dict() if isinstance(cursor, ScanCursor) else dict(cursor)
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(value: str | dict[str, Any] | None) -> ScanCursor:
    if isinstance(value, dict):
        return ScanCursor.from_dict(value)
    if not value:
        return ScanCursor()
    try:
        padded = value + "=" * (-len(value) % 4)
        return ScanCursor.from_dict(json.loads(base64.urlsafe_b64decode(padded).decode()))
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("invalid retrieval scan cursor") from None


def _pages_numbers(pages: Sequence[dict[str, Any]]) -> list[int]:
    return sorted({int(p.get("page_number", p.get("page", 0))) for p in pages})


def build_range_shards(
    pages: Sequence[dict[str, Any]],
    strategy_versions: Iterable[str] = ("lexical-v2", "formula-v2", "table-v2", "complete_scope-v2"),
    pages_per_range: int = 4,
) -> list[RangeShard]:
    """Build stable strategy x range shards.

    Empty page gaps are retained in the range bounds, so a later page insertion does not change
    the meaning of an already persisted range.  Ranges are ordered by strategy then page.
    """
    if pages_per_range < 1:
        raise ValueError("pages_per_range must be positive")
    numbers = _pages_numbers(pages)
    if not numbers:
        return []
    result: list[RangeShard] = []
    for strategy in dict.fromkeys(str(s) for s in strategy_versions):
        for offset in range(0, len(numbers), pages_per_range):
            selected = numbers[offset:offset + pages_per_range]
            range_key = f"p{selected[0]}-p{selected[-1]}"
            result.append(RangeShard(str(strategy), range_key, selected[0], selected[-1], tuple(selected)))
    return result


def plan_rule_ranges(pages: Sequence[dict[str, Any]], semantics: dict[str, Any],
                     pages_per_range: int = 4) -> list[RangeShard]:
    """Freeze the declared scope; section labels are applied only for an explicit section scope."""
    scope = semantics.get("scope") or {"mode": "attachment"}
    mode = scope.get("mode", "attachment")
    if mode == "attachment":
        selected = list(pages)
    elif mode == "explicit_ranges":
        selected = [p for p in pages if any(
            int(r["start"]) <= int(p.get("page_number", p.get("page", 0))) <= int(r["end"])
            for r in scope.get("ranges", []))]
    elif mode == "sections":
        sections = set(scope.get("section_ids") or [])
        selected = [p for p in pages if sections.intersection(
            p.get("section_ids") or [p.get("section_id")])]
    else:
        raise ValueError("unknown rule scope")
    return build_range_shards(selected, pages_per_range=pages_per_range)


def _range_candidates(field_spec: FieldSpec, page: dict, strategy: str, *,
                      window_chars: int, revision: str, rule_hash: str,
                      formula_patterns: Sequence[str], table_patterns: Sequence[str]) -> list[Candidate]:
    """Complete-scope emits every source window; labels merely prioritize the fast path."""
    patterns = formula_patterns if strategy.startswith("formula") else table_patterns
    compiled = [re.compile(pattern) for pattern in patterns]
    candidates = []
    segments = build_segments(page)
    raw_blocks = list(page.get("text_blocks") or [])
    barriers = {i for i, block in enumerate(raw_blocks)
                if block.get("source") not in (None, "native") or block.get("label") == "table"}

    def source_positions(segment: Any) -> list[int]:
        sid = str(segment.segment_id)
        return [i for i, block in enumerate(raw_blocks)
                if (block.get("block_id") or block.get("segment_id")) and
                (sid == "block:" + str(block.get("block_id") or block.get("segment_id"))
                 or sid.startswith("block:" + str(block.get("block_id") or block.get("segment_id")) + ":")
                 or sid.endswith(":" + str(block.get("block_id") or block.get("segment_id"))))]
    windows: list[tuple[int, list[tuple[Any, int, str]]]] = []
    pending: list[tuple[Any, int, str]] = []
    pending_index = 0

    def flush_window() -> None:
        nonlocal pending
        if pending:
            windows.append((pending_index, pending))
            pending = []

    for i, segment in enumerate(segments):
        if strategy.startswith("formula") and not any(p.search(segment.text) for p in compiled):
            continue
        if strategy.startswith("table") and not (segment.table or any(p.search(segment.text) for p in compiled)):
            continue
        packable = (strategy.startswith("complete_scope") and segment.source == "native"
                    and not segment.table and segment.original_source != "table"
                    and len(segment.text) <= window_chars)
        if packable:
            if pending:
                previous = pending[-1][0]
                previous_positions, positions = source_positions(previous), source_positions(segment)
                crosses_barrier = bool(barriers) and (not previous_positions or not positions or any(
                    min(previous_positions) < barrier < max(positions) for barrier in barriers))
                length = sum(len(part) for _, _, part in pending) + len(pending) + len(segment.text)
                if (previous.page_number != segment.page_number
                        or previous.original_source != segment.original_source or crosses_barrier or length > window_chars):
                    flush_window()
            if not pending:
                pending_index = i
            pending.append((segment, 0, segment.text))
            continue
        flush_window()
        # Long source spans retain the original independently recoverable windows. Every
        # original character is retained, while OCR and table identities remain separate.
        for offset in range(0, len(segment.text), window_chars):
            text = segment.text[offset:offset + window_chars]
            windows.append((i, [(segment, offset, text)]))
    flush_window()
    for index, parts in windows:
        segment = parts[0][0]
        text = "\n".join(part for _, _, part in parts)
        context = " | ".join(filter(None, [segment.context_hint,
            *(s.text for s in segments[max(0, index - 2):index])]))
        hints = ["complete_scope_check"] if strategy.startswith("complete_scope") else [strategy.split("-v")[0]]
        if len(parts) > 1:
            hints.append("adjacent_native_segments")
        if segment.source == "ocr":
            hints.append("ocr")
        if segment.table:
            hints.append("table")
        boxes = [part.bbox for part, _, _ in parts if part.bbox]
        bbox = None if not boxes or len(boxes) != len(parts) else [
            min(box[0] for box in boxes), min(box[1] for box in boxes),
            max(box[2] for box in boxes), max(box[3] for box in boxes)]
        candidate = Candidate(field_spec.field_key, segment.page_number, text,
            [label for label in field_spec.labels if label in normalize(text)],
            find_numbers("\n".join(normalize(part) for _, _, part in parts)),
            find_units(normalize(text), field_spec.extra_units),
            segment.source, bbox, segment.table, "weak", hints,
            [f"{part.segment_id}@{offset}:{offset + len(fragment)}" for part, offset, fragment in parts], context)
        candidate.stable_key = stable_candidate_key(revision, rule_hash, candidate, strategy)
        candidate.original_source = segment.original_source
        candidates.append(candidate)
    return candidates


@dataclass
class ScanBatch:
    candidates: list[Candidate]
    cursor: ScanCursor
    scanned_pages: list[int]
    done: bool
    unreadable_pages: list[int] = field(default_factory=list)


def scan_candidates(
    field_spec: FieldSpec,
    pages: Sequence[dict[str, Any]],
    shard: RangeShard,
    *,
    cursor: ScanCursor | dict[str, Any] | None = None,
    page_limit: int = 1,
    window_chars: int = 300,
    content_revision: str = "",
    rule_semantic_hash: str = "",
    formula_patterns: Sequence[str] = (),
    table_patterns: Sequence[str] = (),
) -> ScanBatch:
    """Scan at most ``page_limit`` pages and return all candidates in that slice.

    ``retrieve`` is called with a generous per-page cap, while the durable pagination boundary is
    the page cursor.  Thus ``max_candidates`` in verifier calls cannot silently remove candidates
    from the scan universe.
    """
    if page_limit < 1:
        raise ValueError("page_limit must be positive")
    if window_chars < 1:
        raise ValueError("window_chars must be positive")
    current = ScanCursor.from_dict(cursor) if isinstance(cursor, dict) else (cursor or ScanCursor())
    if current.revision and current.revision != content_revision:
        raise ValueError("scan cursor belongs to a different content revision")
    if current.range_key and current.range_key != shard.range_key:
        raise ValueError("scan cursor belongs to a different range")
    ordered = sorted(
        (p for p in pages if shard.contains(int(p.get("page_number", p.get("page", 0))))),
        key=lambda p: int(p.get("page_number", p.get("page", 0))),
    )
    start = min(current.page_index, len(ordered))
    selected = ordered[start:start + page_limit]
    all_candidates: list[Candidate] = []
    unreadable: list[int] = []
    for page in selected:
        if shard.strategy_version.startswith(("complete_scope", "formula", "table")):
            all_candidates.extend(_range_candidates(field_spec, page, shard.strategy_version,
                window_chars=window_chars, revision=content_revision, rule_hash=rule_semantic_hash,
                formula_patterns=formula_patterns, table_patterns=table_patterns))
            if not page_readable(page):
                unreadable.append(int(page.get("page_number", page.get("page", 0))))
            continue
        result = retrieve(
            field_spec,
            [page],
            # A scan page is the durable pagination unit; candidate caps belong to the
            # judgement batch and must never remove items from the scan universe.
            max_candidates=None,
            window_chars=window_chars,
            content_revision=content_revision,
            rule_semantic_hash=rule_semantic_hash,
            strategy_version=shard.strategy_version,
        )
        all_candidates.extend(result.candidates)
        unreadable.extend(result.unreadable_pages)
    next_index = start + len(selected)
    done = next_index >= len(ordered)
    next_cursor = ScanCursor(
        revision=current.revision or content_revision,
        page_index=next_index,
        segment_index=0,
        range_key=shard.range_key,
        done=done,
    )
    return ScanBatch(all_candidates, next_cursor, [int(p.get("page_number", p.get("page", 0))) for p in selected], done, unreadable)


def candidate_set_digest(candidates: Iterable[Candidate]) -> str:
    keys = sorted(c.stable_key or _fallback_candidate_digest(c) for c in candidates)
    return hashlib.sha256("\n".join(keys).encode()).hexdigest()


def _fallback_candidate_digest(candidate: Candidate) -> str:
    return hashlib.sha256(f"{candidate.page_number}|{candidate.source}|{candidate.text}".encode()).hexdigest()


# Names used by integration code and older P06 drafts.
plan_ranges = build_range_shards
scan_range = scan_candidates

