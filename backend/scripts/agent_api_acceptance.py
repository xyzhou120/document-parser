"""Opt-in live API acceptance, retaining isolated test records and honest evidence.

Uses a real dev PDF for extraction/incremental reuse. Review CRUD is tested on
a synthetic PDF; automated test submissions must never become human gold.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import pymupdf
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.models.database import Document, ExtractedFieldModel, ParseRun  # noqa: E402
from app.storage import AsyncSessionLocal, close_db, minio_client  # noqa: E402


async def request(client, method, path, **kwargs):
    response = await client.request(method, path, **kwargs)
    response.raise_for_status()
    return response.json()


async def wait_run(client, run_id, target):
    for _ in range(300):
        result = await request(client, "GET", f"/api/v1/parse/runs/{run_id}")
        target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        if result["status"] in {"COMPLETED", "FAILED"}:
            if result["status"] == "FAILED":
                raise ValueError("Live extraction task failed; inspect retained result")
            return result
        await asyncio.sleep(3)
    raise TimeoutError("Run still pending; result and run ID retained")


async def review_acceptance(client, directory):
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=300, height=300)
        page.insert_text((30, 40), "Synthetic review test: actual holes 321")
        source = pdf.tobytes()
    digest = hashlib.sha256(source).hexdigest()
    key = f"acceptance/synthetic-review-{uuid4()}.pdf"
    await asyncio.to_thread(minio_client.upload, key, source)
    async with AsyncSessionLocal() as db:
        doc = Document(file_name="synthetic-review-acceptance.pdf", original_name="synthetic-review-acceptance.pdf",
                       file_path=key, file_size=len(source), file_hash=digest, mime_type="application/pdf",
                       mine_id="synthetic-acceptance", mine_name="合成验收数据（非矿上报）",
                       checklist_type="synthetic_acceptance", report_period="2026-10", parse_status="COMPLETED")
        db.add(doc)
        await db.flush()
        run = ParseRun(document_id=doc.document_id, status="COMPLETED", fields_extracted=1,
                       extraction_profile={"synthetic_acceptance": True}, quality_status="REVIEW_REQUIRED")
        db.add(run)
        await db.flush()
        field = ExtractedFieldModel(parse_run_id=run.parse_run_id, document_id=doc.document_id,
                                    field_key="count", field_name="合成孔数", field_value="999",
                                    confidence=None, extraction_method="synthetic_test", instances=[],
                                    validation_status="unresolved", extraction_detail={}, required=True)
        db.add(field)
        await db.commit()
        run_id = str(run.parse_run_id)
    path = f"/api/v1/reviews/runs/{run_id}/fields/count"
    payload = {"action": "CORRECT", "expected_revision": 0,
               "reviewer": "AUTOMATED_SYNTHETIC_API_TEST_NOT_HUMAN_GOLD", "reason": "合成数据接口验收",
               "file_hash": digest, "facts": [{"raw_value": "321", "value": "321", "unit": "个",
               "value_kind": "actual", "source_role": "synthetic", "dimensions": {},
               "evidence": [{"page_number": 1, "bbox": [20, 20, 290, 70], "quote": "actual holes 321"}]}]}
    corrected = await request(client, "POST", path, json=payload)
    stale = await client.post(path, json=payload)
    assert stale.status_code == 409
    accepted = await request(client, "POST", path, json={**payload, "action": "ACCEPT",
                             "expected_revision": 1, "facts": []})
    rejected = await request(client, "POST", path, json={**payload, "action": "REJECT",
                             "expected_revision": 2, "facts": [], "reason": "合成验收终态，禁止当作人工金标准"})
    history = await request(client, "GET", path)
    export = await request(client, "GET", f"/api/v1/reviews/runs/{run_id}/export")
    assert corrected["revision"] == 1 and accepted["revision"] == 2 and rejected["revision"] == 3
    assert history["automatic"]["field_value"] == "999" and history["automatic"]["confidence"] is None
    assert export["facts"][0]["review_status"] == "REJECTED"
    protected = False
    async with AsyncSessionLocal() as db:
        try:
            await db.execute(text("UPDATE field_review_revisions SET reason = 'forbidden-test' WHERE review_id = :review_id"),
                             {"review_id": corrected["review_id"]})
        except DBAPIError as exc:
            protected = "append-only" in str(exc)
        finally:
            await db.rollback()
    assert protected, "PostgreSQL must block direct mutations of audit revisions"
    result = {"synthetic_only": True, "run_id": run_id, "correct_accept_reject": True,
              "optimistic_conflict": 409, "automatic_value_unchanged": True, "database_append_only": protected,
              "history": history, "export": export}
    (directory / "synthetic_review_acceptance.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return {k: v for k, v in result.items() if k not in {"history", "export"}}


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://127.0.0.1:8001")
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--review-only", action="store_true")
    args = parser.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=False)
    summary = {"started_at": datetime.now(UTC).isoformat(), "transport": "HTTP_API_CELERY_WORKER",
               "accuracy_status": "UNVERIFIED", "human_gold": False}
    try:
        async with httpx.AsyncClient(base_url=args.api, timeout=180) as client:
            if not args.review_only:
                checklist = "agent_acceptance_" + uuid4().hex[:12]
                await request(client, "POST", "/api/v1/rules/checklist-types", json={"checklist_type": checklist,
                              "checklist_name": "自动接口验收清单（隔离测试）", "description": "真实开发样本、未人工标注",
                              "is_active": True, "sort_order": 999})
                rules = await request(client, "GET", "/api/v1/rules", params={"checklist_type": "gas_report_agent"})
                by_key = {r["field_key"]: r for r in rules}
                async def add_rule(key):
                    row = {k: v for k, v in by_key[key].items() if k not in {"rule_id", "version", "created_at", "updated_at"}}
                    row.update(checklist_type=checklist, enabled=True)
                    return await request(client, "POST", "/api/v1/rules", json=row)
                await add_rule("actual_hole_count")
                await add_rule("evaluation_unit_range")
                manifest = json.loads((ROOT / "output/agent_evaluation/manifest.json").read_text(encoding="utf-8"))
                sample = next(d for d in manifest["documents"] if d["id"] == 131)
                source = ROOT / sample["path"]
                content = source.read_bytes()
                assert hashlib.sha256(content).hexdigest() == sample["sha256"]
                upload = await request(client, "POST", "/api/v1/parse/upload-and-parse",
                                       data={"mine_id": "dev-sample-131", "mine_name": sample["mine"],
                                             "checklist_type": checklist, "reporting_period": "2026-10",
                                             "extraction_engine": "checklist_agent"},
                                       files={"file": (source.name, content, "application/pdf")})
                (args.out_dir / "upload.json").write_text(json.dumps(upload), encoding="utf-8")
                full = await wait_run(client, upload["parse_run_id"], args.out_dir / "full.json")
                assert full["artifact_id"] and full["mean_confidence"] is None
                await add_rule("residual_gas_pressure_measured")
                child = await request(client, "POST", f"/api/v1/parse/documents/{upload['document_id']}/extract", json={})
                (args.out_dir / "incremental_submission.json").write_text(json.dumps(child), encoding="utf-8")
                incremental = await wait_run(client, child["parse_run_id"], args.out_dir / "incremental.json")
                assert incremental["artifact_id"] == full["artifact_id"]
                assert incremental["cache_status"] == "ARTIFACT_HIT"
                assert incremental["parent_parse_run_id"] == full["parse_run_id"]
                inherited = [f for f in incremental["fields"] if f["field_key"] != "residual_gas_pressure_measured"]
                assert len(inherited) == len(full["fields"])
                assert all((f["extraction_detail"] or {}).get("provenance") == "inherited" for f in inherited)
                runtime = incremental["extraction_profile"]["agent_runtime"]
                assert list(runtime["field_coverage"]) == ["residual_gas_pressure_measured"]
                summary.update(checklist_type=checklist, full_run_id=full["parse_run_id"], child_run_id=incremental["parse_run_id"],
                               same_artifact=True, only_new_field_executed=True, inherited_fields=len(inherited),
                               full_fields_extracted=full["fields_extracted"], incremental_fields_extracted=incremental["fields_extracted"])
            summary["reviews"] = await review_acceptance(client, args.out_dir)
        summary["status"] = "PASSED"
    finally:
        (args.out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        await close_db()
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
