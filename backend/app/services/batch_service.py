"""Batch state and export helpers shared by the API and offline verification."""

import csv
import io
import json
from collections import Counter

from app.models.database import Document, ParseRun

ACTIVE_RUN_STATES = {"PENDING", "RUNNING", "PROCESSING", "RETRYING"}


def item_status(item, run=None):
    if run is not None:
        return "PARSE_FAILED" if run.status == "FAILED" else run.status
    return item.status


def batch_summary(items, runs):
    counts = Counter(item_status(item, runs.get(item.parse_run_id)) for item in items)
    total = len(items)
    if not total:
        status = "PENDING"
    elif any(counts[s] for s in ACTIVE_RUN_STATES | {"UPLOADING"}):
        status = "PROCESSING"
    elif counts["PARSE_FAILED"] or counts["UPLOAD_FAILED"]:
        status = "PARTIAL_FAILED"
    elif counts["CANCELLED"] == total:
        status = "CANCELLED"
    else:
        status = "COMPLETED"
    terminal = sum(counts[s] for s in {"COMPLETED", "PARSE_FAILED", "UPLOAD_FAILED", "CANCELLED"})
    return {
        "status": status,
        "total": total,
        "completed": counts["COMPLETED"],
        "failed": counts["PARSE_FAILED"] + counts["UPLOAD_FAILED"],
        "cancelled": counts["CANCELLED"],
        "terminal": terminal,
        "progress_percent": round(100 * terminal / total, 1) if total else 0,
        "counts": dict(counts),
    }


class BatchUploadSession:
    """Attach original document/run before the uploader's transaction commits.

    The existing upload API commits internally. Observing its additions allows
    the batch item and document to commit atomically; a concurrent retry waits
    on the item row lock and then returns the existing identifiers.
    """

    def __init__(self, db, item):
        self.db, self.item = db, item
        self.document = self.run = None

    def __getattr__(self, name):
        return getattr(self.db, name)

    def add(self, obj):
        self.db.add(obj)
        if isinstance(obj, Document):
            self.document = obj
        elif isinstance(obj, ParseRun):
            self.run = obj

    async def flush(self, *args, **kwargs):
        await self.db.flush(*args, **kwargs)
        self._link()

    def _link(self):
        if self.document and self.document.document_id:
            self.item.document_id = self.document.document_id
            self.item.file_size = self.document.file_size
        if self.run and self.run.parse_run_id:
            self.item.parse_run_id = self.run.parse_run_id
            self.item.status = "PENDING"

    async def commit(self):
        await self.db.flush()
        self._link()
        if self.run and self.run.status == "FAILED":
            self.item.status = "PARSE_FAILED"
            self.item.error_message = self.run.error_message
        await self.db.commit()


def csv_safe(value):
    text = (
        json.dumps(value, ensure_ascii=False, default=str)
        if isinstance(value, dict | list)
        else str(value if value is not None else "")
    )
    # Prevent a downstream spreadsheet from executing an untrusted PDF value.
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) else text


def facts_csv(rows):
    output = io.StringIO(newline="")
    columns = [
        "parse_run_id",
        "document_id",
        "file_name",
        "mine_id",
        "mine_name",
        "checklist_type",
        "reporting_period",
        "status",
        "field_key",
        "field_name",
        "fact_id",
        "value",
        "unit",
        "identity",
        "confidence",
        "confidence_kind",
        "risks",
        "evidence",
        "export_note",
        "missing_field",
        "fact",
    ]
    writer = csv.DictWriter(output, fieldnames=columns)
    writer.writeheader()
    for row in rows:
        fact = row.get("fact", {})
        confidence = fact.get("confidence") or {}
        projected = {
            **fact,
            "identity": {
                "dimensions": fact.get("dimensions"),
                "value_kind": fact.get("value_kind"),
                "source_role": fact.get("source_role"),
            },
            "risks": fact.get("risk", []),
            "confidence": confidence.get("score") if isinstance(confidence, dict) else confidence,
            "confidence_kind": (
                confidence.get("kind") if isinstance(confidence, dict) else "unknown"
            ),
        }
        writer.writerow({key: csv_safe(row.get(key, projected.get(key))) for key in columns})
    return "\ufeff" + output.getvalue()
