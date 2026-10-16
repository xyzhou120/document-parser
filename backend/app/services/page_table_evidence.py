"""Strict table-cell evidence checks for page-batched extraction.

The model may propose a table relation, but this module accepts it only after
locating the claimed row and column in the immutable page unit.  A number that
appears elsewhere in the table therefore cannot satisfy the fact.
"""

from __future__ import annotations

import html
import json
from html.parser import HTMLParser
from typing import Any, cast

from app.services.candidate_retrieval import (
    NUMERIC_TYPES,
    find_units,
    normalize,
    normalize_unit,
    value_in_text,
)


class _TableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self.unsupported_spans = False
        self.table_count = 0

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "table":
            self.table_count += 1
        elif tag.lower() == "tr":
            self._row = []
        elif tag.lower() in {"td", "th"} and self._row is not None:
            self.unsupported_spans |= any(key in {"rowspan", "colspan"} and str(value) != "1"
                                          for key, value in attrs)
            self._cell = []

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in {"td", "th"} and self._cell is not None and self._row is not None:
            self._row.append(normalize(html.unescape("".join(self._cell))))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None


def _cell(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("text", "")
    return normalize(value) if isinstance(value, str | int | float) else ""


def _json_tables(text: str, tables: list[dict]) -> list[list[list[str]]]:
    result = []
    records: list[dict] = []
    for line in str(text or "").splitlines():
        try:
            item = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(item, dict):
            records.append(item)
    def header_columns(value: Any) -> list[Any] | None:
        if isinstance(value, dict):
            value = value.get("columns") or value.get("names")
        return cast(list[Any], value) if isinstance(value, list) else None

    if records:
        header = next((columns for item in records
                       if (columns := header_columns(item.get("header"))) is not None), None)
        if header is None:
            header = next((columns for item in tables if isinstance(item, dict)
                           if (columns := header_columns(item.get("header"))) is not None), None)
        json_rows: list[tuple[Any, Any]] = [(item["row_index"], item.get("row")) for item in records
                if isinstance(item.get("row_index"), int) and isinstance(item.get("row"), list)]
        if header is not None and json_rows and len({index for index, _ in json_rows}) == len(json_rows):
            by_index = dict(json_rows)
            result.append([[_cell(v) for v in header],
                           *[[_cell(v) for v in by_index.get(index, [])]
                             for index in range(max(by_index) + 1)]])
    for metadata in tables:
        if not isinstance(metadata, dict):
            continue
        header_value = header_columns(metadata.get("header"))
        metadata_rows = metadata.get("rows")
        header = cast(list[Any], header_value) if isinstance(header_value, list) else None
        if isinstance(header, list) and isinstance(metadata_rows, list):
            result.append([[_cell(v) for v in header],
                           *[[_cell(v) for v in row] for row in metadata_rows if isinstance(row, list)]])
    return result


def _matrices(unit: dict) -> list[list[list[str]]]:
    text = str(unit.get("text") or "")
    matrices = _json_tables(text, list(unit.get("tables") or []))
    parser = _TableParser()
    parser.feed(text)
    if parser.rows and not parser.unsupported_spans and parser.table_count <= 1:
        matrices.append(parser.rows)
    # Independent image transcription may retain a Markdown/tab-separated
    # grid. Whitespace prose has no reliable column boundaries and is ignored.
    transcript = str(unit.get("transcription") or "")
    if transcript:
        rows = []
        for line in transcript.splitlines():
            if "\t" in line:
                row = line.split("\t")
            elif "|" in line:
                row = line.strip().strip("|").split("|")
            else:
                continue
            row = [normalize(cell) for cell in row]
            if all(not cell.strip("-:") for cell in row):
                continue
            if len(row) > 1:
                rows.append(row)
        if rows and len({len(row) for row in rows}) == 1:
            matrices.append(rows)
    unique = []
    seen = set()
    for matrix in matrices:
        key = json.dumps(matrix, ensure_ascii=False, sort_keys=True)
        if key not in seen:
            seen.add(key)
            unique.append(matrix)
    return unique


def _relation(decision: dict) -> dict:
    value = decision.get("table_relation") or decision.get("tableRelation") or {}
    return dict(value) if isinstance(value, dict) else {}


def table_fact_support(unit: dict, decision: dict, rule: Any) -> dict:
    """Verify a decision's value, unit and identities against one exact cell.

    ``row_index`` is zero based and includes the header row.  ``column_index``
    is zero based.  Missing or ambiguous relation metadata returns ``usable``
    false with explicit issues; it never downgrades to a whole-table literal.
    """
    relation = _relation(decision)
    issues: list[str] = []
    attrs: dict[str, dict] = {}
    if not relation:
        return {"usable": False, "attributes": attrs, "issues": ["TABLE_RELATION_MISSING"]}
    try:
        row_index = relation["row_index"]
        column_index = relation["column_index"]
        if (not isinstance(row_index, int) or isinstance(row_index, bool)
                or not isinstance(column_index, int) or isinstance(column_index, bool)):
            raise ValueError("coordinates must be integers")
    except (KeyError, TypeError, ValueError):
        return {"usable": False, "attributes": attrs, "issues": ["TABLE_COORDINATES_INVALID"]}
    matrices = _matrices(unit)
    matches = []
    for matrix in matrices:
        if not matrix or row_index <= 0 or row_index >= len(matrix):
            continue
        header, row = matrix[0], matrix[row_index]
        if column_index < 0 or column_index >= len(header) or column_index >= len(row):
            continue
        column_header = normalize(relation.get("column_header") or "")
        row_quote = normalize(relation.get("row_quote") or "")
        if not column_header or normalize(header[column_index]) != column_header:
            continue
        row_text = " ".join(row)
        if not row_quote or row_quote != normalize(row_text):
            continue
        matches.append((header, row, row[column_index]))
    if len(matches) != 1:
        issues.append("TABLE_RELATION_UNBOUND" if not matches else "TABLE_RELATION_AMBIGUOUS")
        return {"usable": False, "attributes": attrs, "issues": issues}
    header, row, cell = matches[0]
    def get_rule(key, default=None):
        return rule.get(key, default) if isinstance(rule, dict) else getattr(rule, key, default)
    numeric = str(get_rule("data_type", "string")).lower() in NUMERIC_TYPES
    value = decision.get("value")
    if not value_in_text(value, cell, numeric):
        issues.append("TABLE_VALUE_CELL_MISMATCH")
    decision_unit = decision.get("unit")
    column_text = header[column_index]
    labels = [get_rule("field_name_cn") or ""]
    synonyms = ((get_rule("strategy") or {}).get("agent_extract") or {}).get("synonyms") or []
    labels.extend([synonyms] if isinstance(synonyms, str) else synonyms)
    if not any(normalize(label) and normalize(label) in normalize(column_text) for label in labels):
        issues.append("TABLE_FIELD_COLUMN_MISMATCH")
    units_in_column = {normalize_unit(found) for found in find_units(
        column_text + " " + cell, (str(decision_unit),) if decision_unit else ())}
    if decision_unit and normalize_unit(decision_unit) not in units_in_column:
        issues.append("TABLE_UNIT_HEADER_MISMATCH")
    if not issues:
        attrs["value"] = {"status": "supported", "value": value, "basis": "table_cell"}
        if decision_unit:
            attrs["unit"] = {"status": "supported", "value": decision_unit, "basis": "table_header_binding"}
        attrs["table_relation"] = {"status": "supported", "value": relation, "basis": "table_cell_binding"}
        kind = str(decision.get("value_kind") or "unknown")
        kind_terms = {"measured": ("测定", "测量", "实测", "检测"), "design": ("设计",),
                      "calculated": ("计算", "推算")}
        if kind == "reported" or any(term in column_text for term in kind_terms.get(kind, ())):
            attrs["value_kind"] = {"status": "supported", "value": kind, "basis": "header"}
        else:
            issues.append("TABLE_VALUE_KIND_HEADER_MISMATCH")
        for key, observed in (decision.get("dimensions") or {}).items():
            if observed and normalize(observed) in {normalize(cell) for cell in row}:
                attrs[f"dimensions.{key}"] = {"status": "supported", "value": observed, "basis": "table_row"}
            else:
                issues.append(f"TABLE_DIMENSION_UNBOUND:{key}")
    return {"usable": not issues, "attributes": attrs, "issues": issues,
            "row": row, "header": header, "cell": cell}


def table_row_support(unit: dict, decision: dict, rule: Any) -> dict:
    """从同一已定位原文行绑定全部配置列，不接受模型自报的其他单元格。"""
    config = rule.get("output_schema", {}) if isinstance(rule, dict) else getattr(rule, "output_schema", {})
    relation = _relation(decision)
    columns = list((config or {}).get("columns") or [])
    identity_keys = list((config or {}).get("row_identity") or [])
    try:
        row_index, column_index = relation["row_index"], relation["column_index"]
        if any(isinstance(index, bool) or not isinstance(index, int) for index in (row_index, column_index)):
            raise ValueError
    except (KeyError, ValueError, TypeError):
        return {"usable": False, "issues": ["TABLE_COORDINATES_INVALID"]}
    matches = []
    for matrix in _matrices(unit):
        if not matrix or not 0 < row_index < len(matrix):
            continue
        header, row = matrix[0], matrix[row_index]
        if column_index >= len(header) or column_index >= len(row) or column_index < 0:
            continue
        if normalize(relation.get("column_header") or "") != normalize(header[column_index]):
            continue
        if normalize(relation.get("row_quote") or "") != normalize(" ".join(row)):
            continue
        if not value_in_text(decision.get("value"), row[column_index], False):
            continue
        matches.append((header, row))
    if len(matches) != 1:
        return {"usable": False, "issues": ["TABLE_ROW_UNBOUND" if not matches else "TABLE_ROW_AMBIGUOUS"]}
    header, row = matches[0]
    issues = []
    cells = {}
    for column in columns:
        key = column.get("key")
        labels = [column.get("header"), *(column.get("aliases") or [])]
        positions = [index for index, heading in enumerate(header)
                     if any(normalize(label) == normalize(heading) for label in labels if label)]
        if len(positions) != 1 or positions[0] >= len(row):
            issues.append(f"TABLE_COLUMN_UNBOUND:{key}")
            continue
        index = positions[0]
        value = row[index]
        if not value and column.get("required", True):
            issues.append(f"TABLE_CELL_BLANK:{key}")
        cells[key] = {"value": value or None, "unit": column.get("unit"),
                      "column_header": header[index], "table_relation": {
                          "row_index": row_index, "column_index": index,
                          "column_header": header[index], "row_quote": " ".join(row)}}
    identity = {key: (cells.get(key) or {}).get("value") for key in identity_keys}
    if not identity_keys or any(not value for value in identity.values()):
        issues.append("TABLE_ROW_IDENTITY_UNBOUND")
    return {"usable": not issues, "issues": issues, "row": {
        "row_key": "|".join(f"{key}={value}" for key, value in identity.items() if value),
        "identity": identity, "cells": cells,
        "status": "DELIVERABLE" if not issues else "REVIEW_REQUIRED",
        "row_index": row_index, "row_quote": " ".join(row)}}

