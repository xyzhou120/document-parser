"""Bridge human review exports into a conservative evaluation annotation draft.

The bridge never creates VERIFIED gold. One API reviewer is evidence of a review
event, not independent adjudication. A field remains DRAFT until a separate
adjudicator completes the existing ``agent_evaluation.py`` validation contract.

Examples:
  backend/.venv/Scripts/python.exe backend/scripts/review_to_annotation.py \
    --manifest output/agent_evaluation/manifest.json \
    --reviews output/agent_evaluation/reviews/131.json \
    --out output/agent_evaluation/annotations-draft.json
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

if __package__:
    from .agent_evaluation import (
        DEFAULT_FIELDS,
        annotation_template,
        normalize_value,
        read_json,
        write_json,
    )
else:
    from agent_evaluation import (
        DEFAULT_FIELDS,
        annotation_template,
        normalize_value,
        read_json,
        write_json,
    )


def _review_rows(value: Any) -> Iterable[dict[str, Any]]:
    """Accept one export response, a list, or a saved API history response."""
    if isinstance(value, list):
        yield from (row for row in value if isinstance(row, dict))
        return
    if not isinstance(value, dict):
        return
    if isinstance(value.get("reviews"), list):
        yield from _review_rows(value["reviews"])
    elif isinstance(value.get("history"), list):
        yield from _review_rows(value["history"])
    elif isinstance(value.get("facts"), list) and value.get("field_key"):
        yield value
    elif isinstance(value.get("facts"), list):
        yield from _review_rows(value["facts"])
    for row in value.get("fields", []) if isinstance(value.get("fields"), list) else []:
        if isinstance(row, dict):
            yield row


def _fact(fact: dict[str, Any]) -> dict[str, Any]:
    """Map review facts to evaluation facts without inferring missing semantics."""
    return {
        "raw_value": str(fact.get("raw_value", fact.get("value", ""))),
        "normalized_value": normalize_value(fact.get("value", fact.get("raw_value", ""))),
        "unit": fact.get("unit"),
        "dimensions": fact.get("dimensions") if isinstance(fact.get("dimensions"), dict) else {},
        "value_kind": fact.get("value_kind", "unknown"),
        "source_role": fact.get("source_role", "unknown"),
        "evidence": [
            {
                "page_number": evidence.get("page_number"),
                "bbox": evidence.get("bbox"),
                "coordinate_system": evidence.get("coordinate_system", "PDF_POINT"),
                "quote": evidence.get("quote"),
                "role": evidence.get("role", "value"),
                "evidence_id": evidence.get("evidence_id"),
            }
            for evidence in fact.get("evidence", [])
            if isinstance(evidence, dict) and evidence.get("bbox") is not None
        ],
    }


def bridge(manifest: dict[str, Any], review_files: list[str | Path]) -> dict[str, Any]:
    """Return a new annotation document with review-backed fields marked DRAFT."""
    by_id = {str(doc["id"]): doc for doc in manifest.get("documents", [])}
    by_hash = {doc.get("sha256"): doc for doc in manifest.get("documents", [])}
    draft: dict[str, Any] = {
        "schema_version": 1,
        "checklist_type": manifest.get("checklist_type"),
        "annotation_status": "DRAFT",
        "draft_policy": {
            "status": "DRAFT",
            "reason": "API review is a single review event; independent adjudication is still required",
            "verified_requires": ["complete=true", "annotator", "different adjudicator", "verified_at"],
            "metrics_allowed": False,
        },
        "generated_at": datetime.now(UTC).isoformat(),
        "documents": [],
    }
    draft["documents"] = annotation_template(manifest, DEFAULT_FIELDS)["documents"]
    docs = {str(doc["id"]): doc for doc in draft["documents"]}
    for file_name in review_files:
        payload = read_json(file_name)
        for row in _review_rows(payload):
            raw_id = row.get("document_id", payload.get("document_id") if isinstance(payload, dict) else None)
            document = by_id.get(str(raw_id))
            if document is None and row.get("file_hash"):
                document = by_hash.get(row["file_hash"])
            if document is None:
                raise ValueError(f"review has no matching manifest document: {raw_id}")
            if row.get("file_hash") != document["sha256"]:
                raise ValueError(f"review file hash mismatch for document {document['id']}")
            field_key = row.get("field_key")
            if not field_key:
                raise ValueError("review row is missing field_key")
            if row.get("action") not in {"ACCEPT", "CORRECT", "REJECT"}:
                raise ValueError("unsupported review action")
            if not isinstance(row.get("revision"), int) or row["revision"] < 1:
                raise ValueError("review row needs a positive revision")
            fields = docs[str(document["id"])]["fields"]
            field = next((item for item in fields if item["field_key"] == field_key), None)
            if field is None:
                field = {
                    "field_key": field_key, "status": "DRAFT", "scope": "whole_document",
                    "scope_pages": [], "complete": False, "annotator": row.get("reviewer", ""),
                    "adjudicator": "", "verified_at": "", "facts": [],
                    "unreadable_evidence": [], "notes": "", "uncertainty": [],
                }
                fields.append(field)
            if (field.get("review_provenance") or {}).get("revision", 0) >= row["revision"]:
                continue
            field["status"] = "DRAFT"
            field["complete"] = False
            field["facts"] = [_fact(fact) for fact in row.get("facts", []) if isinstance(fact, dict)]
            if row["action"] == "REJECT":
                if field["facts"]:
                    raise ValueError("REJECT review cannot contain accepted facts")
                field["notes"] = "审核拒绝不等于原文不存在，禁止转换为ABSENT。"
            field["pending_evidence"] = [
                evidence for fact in row.get("facts", []) if isinstance(fact, dict)
                for evidence in fact.get("evidence", []) if isinstance(evidence, dict) and evidence.get("bbox") is None
            ]
            field["annotator"] = row.get("reviewer", field.get("annotator", ""))
            field["adjudicator"] = ""
            field["verified_at"] = ""
            field["uncertainty"] = [
                "仅有一名审核人记录，未完成独立裁决",
                "请确认全文/指定页范围内是否还有其他实例",
            ]
            field["notes"] = (field.get("notes", "") + " " + row.get("reason", "")).strip()
            field["review_provenance"] = {
                "review_id": row.get("review_id"), "revision": row.get("revision"),
                "action": row.get("action"), "reviewer": row.get("reviewer"),
                "file_hash": row.get("file_hash", document["sha256"]),
            }
    return draft


def main() -> None:
    parser = argparse.ArgumentParser(description="将人工复核导出转换为DRAFT评测标注")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--reviews", nargs="+", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--overwrite", action="store_true", help="明确允许覆盖输出草稿；不推荐覆盖人工标注")
    args = parser.parse_args()
    if Path(args.out).exists() and not args.overwrite:
        parser.error("输出已存在；请使用新文件名保护人工标注")
    result = bridge(read_json(args.manifest), args.reviews)
    write_json(args.out, result)
    print(json.dumps({"out": args.out, "documents": len(result["documents"]), "status": "DRAFT"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
