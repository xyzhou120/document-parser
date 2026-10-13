"""Run an isolated, page-batched extraction against frozen production inputs.

The source database is read-only.  A document, immutable artifact/revision and
the source run's frozen rule snapshots are copied into the explicitly allowlisted
G1 database.  Model calls and their ledger are written only to that database.
This script intentionally does not dispatch Celery or mutate the source head.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pymupdf
from openai import AsyncOpenAI
from sqlalchemy import NullPool, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.config import settings  # noqa: E402
from app.models.content import ContentRevision, DocumentProcessingHead  # noqa: E402
from app.models.database import (  # noqa: E402
    Document,
    DocumentArtifact,
    ExtractedFieldModel,
    ParseRun,
)
from app.models.execution import ParseFieldJob, ParseModelCall  # noqa: E402
from app.rules.spec import RuleSpec  # noqa: E402
from app.rules.strategies.llm import LLMStrategy  # noqa: E402
from app.services.call_ledger import CallLedger  # noqa: E402
from app.services.content_adapter import ContentAdapter  # noqa: E402
from app.services.content_service import ContentService  # noqa: E402
from app.services.execution import JobRepository  # noqa: E402
from app.services.model_gateway import GatewayPolicy, ModelGateway  # noqa: E402
from app.services.reliable_extraction_service import ReliableExtractionService  # noqa: E402
from app.services.reliable_field_executor import ReliableFieldExecutorConfig  # noqa: E402
from app.services.reliable_page_executor import ReliablePageExecutor  # noqa: E402
from app.services.service_circuit import (  # noqa: E402
    MemoryCircuitStore,
    ServiceCircuit,
    service_key,
)
from app.storage.minio_client import minio_client  # noqa: E402

TARGET_DATABASE = "document_parser_v2_g1_20261012_a8d41c6f"
MAX_RUN_SECONDS = 300


def _rule(snapshot: dict) -> RuleSpec:
    allowed = set(RuleSpec.__dataclass_fields__)
    return RuleSpec(**{key: deepcopy(value) for key, value in snapshot.items() if key in allowed})


def _url_for(name: str) -> str:
    db = settings.database
    from urllib.parse import quote

    return (f"postgresql+asyncpg://{quote(db.user, safe='')}:{quote(db.password, safe='')}"
            f"@{db.host}:{db.port}/{quote(name, safe='')}")


class LocalImageProvider:
    """Render original source locally; no evidence images are uploaded."""

    def __init__(self, document, pages: list[dict], source_pdf: Path, workspace: Path):
        self.document = document
        self.pages = {int(page["page_number"]): page for page in pages}
        self.source_pdf = source_pdf
        self.workspace = workspace

    async def __call__(self, document, pages, candidate):
        page_number = int(candidate["page_number"])
        page = self.pages[page_number]
        width, height = float(page.get("width") or 0), float(page.get("height") or 0)
        requested_bbox = candidate.get("bbox")
        segment_ids = {item.get("segment_id") for item in candidate.get("source_segments") or []}
        source_blocks = [block for block in page.get("text_blocks") or []
                         if block.get("block_id") in segment_ids]
        pdf_coordinates = bool(source_blocks) and all(
            block.get("coordinate_system") == "PDF_POINT" for block in source_blocks)
        # Unknown/PIXEL coordinates cannot safely be interpreted as PDF points.
        # Read the full original page in that case and record the actual bounds.
        bbox = requested_bbox if requested_bbox and pdf_coordinates else [0.0, 0.0, width, height]
        clip = [float(value) for value in bbox]
        if len(clip) != 4 or not (0 <= clip[0] < clip[2] <= width and 0 <= clip[1] < clip[3] <= height):
            raise ValueError(f"invalid PDF_POINT bbox for page {page_number}")
        def render():
            with pymupdf.open(self.source_pdf) as pdf:
                return bytes(pdf[page_number - 1].get_pixmap(dpi=120, clip=pymupdf.Rect(clip)).tobytes("png"))
        image = await asyncio.to_thread(render)
        image_hash = hashlib.sha256(image).hexdigest()
        path = self.workspace / f"page-{page_number}-{image_hash[:16]}.png"
        await asyncio.to_thread(path.write_bytes, image)
        return image, {"source_file_hash": document.file_hash, "image_hash": image_hash,
                       "page_number": page_number, "bbox": clip, "coordinate_system": "PDF_POINT",
                       "coordinate_origin": "MUPDF_ROTATED_TOP_LEFT", "local_path": str(path),
                       "requested_bbox": requested_bbox,
                       "source_review_status": "UNVERIFIED"}


async def _copy_source(source_db, target_db, source_run_id: UUID):
    """Copy a frozen run input under fresh identities, without source writes."""
    async with source_db() as db, db.begin():
        await db.execute(text("SET TRANSACTION READ ONLY"))
        source_run = await db.get(ParseRun, source_run_id)
        if source_run is None:
            raise ValueError(f"source run not found: {source_run_id}")
        document = await db.get(Document, source_run.document_id)
        revision = await db.get(ContentRevision, source_run.content_revision_id)
        artifact = await db.get(DocumentArtifact, revision.artifact_id) if revision else None
        jobs = list((await db.execute(select(ParseFieldJob).where(
            ParseFieldJob.run_id == source_run_id).order_by(ParseFieldJob.field_key))).scalars())
        if document is None or artifact is None or revision is None or not jobs:
            raise ValueError(f"source run {source_run_id} lacks frozen document/artifact/revision/rules")
        await ContentService().read_revision(db, revision.revision_id)
        rules = [_rule(deepcopy((job.checkpoint or {}).get("rule_snapshot") or {})) for job in jobs]
        source = {"run": source_run, "document": document, "artifact": artifact,
                  "revision": revision, "rules": rules, "jobs": jobs}

    target_doc_id, target_artifact_id, target_revision_id = uuid4(), uuid4(), uuid4()
    target_document = Document(document_id=target_doc_id, file_name=document.file_name,
        original_name=document.original_name, file_path=document.file_path,
        file_size=document.file_size, file_hash=document.file_hash, mime_type=document.mime_type,
        mine_id=document.mine_id, mine_name=document.mine_name, checklist_type=document.checklist_type,
        report_period=document.report_period, page_count=document.page_count, parse_status="PENDING")
    target_artifact = DocumentArtifact(artifact_id=target_artifact_id, document_id=target_doc_id,
        file_hash=artifact.file_hash, preprocess_profile_version=artifact.preprocess_profile_version,
        status=artifact.status, pages=deepcopy(artifact.pages), quality=deepcopy(artifact.quality))
    # Compute immutable digests against the target artifact identity, exactly as
    # ContentService.read_revision will do in the isolated database.
    adapted = ContentAdapter().adapt(target_artifact)
    if adapted.content_digest != revision.content_digest or adapted.coverage_digest != revision.coverage_digest:
        raise ValueError("copied artifact must exactly retain the source revision content and coverage")
    target_revision = ContentRevision(revision_id=target_revision_id, document_id=target_doc_id,
        artifact_id=target_artifact_id, parent_revision_id=None, origin_source_revision_id=None,
        origin_event_id=f"compare:{source_run_id}", file_hash=artifact.file_hash,
        content_digest=adapted.content_digest, preprocess_manifest=deepcopy(revision.preprocess_manifest),
        adapter_version=adapted.adapter_version, source_kind=revision.source_kind,
        change_kind="comparison", coverage_manifest=deepcopy(adapted.coverage_manifest),
        coverage_digest=adapted.coverage_digest)
    async with target_db() as db, db.begin():
        db.add(target_document)
        await db.flush()
        db.add(target_artifact)
        await db.flush()
        db.add(target_revision)
        await db.flush()
        db.add(DocumentProcessingHead(document_id=target_doc_id,
                        active_content_revision_id=target_revision_id, current_parse_run_id=None,
                        cas_version=0, invalidation_watermark=0))
        await db.flush()
    return source, target_doc_id, target_revision_id, target_document, adapted.pages


async def _run_one(source_db, target_db, source_run_id: UUID, out_dir: Path, model_client):
    started = time.monotonic()
    source, target_doc_id, revision_id, target_document, pages = await _copy_source(source_db, target_db, source_run_id)
    pdf_path = out_dir / f"{source['document'].file_hash}.pdf"
    if not pdf_path.exists():
        source_bytes = await asyncio.to_thread(minio_client.download, source["document"].file_path)
        if hashlib.sha256(source_bytes).hexdigest() != source["document"].file_hash:
            raise ValueError(f"source file hash mismatch for {source_run_id}")
        await asyncio.to_thread(pdf_path.write_bytes, source_bytes)
    if hashlib.sha256(await asyncio.to_thread(pdf_path.read_bytes)).hexdigest() != source["document"].file_hash:
        raise ValueError(f"cached source file hash mismatch for {source_run_id}")
    image_provider = LocalImageProvider(target_document, pages, pdf_path, out_dir)
    service = ReliableExtractionService()
    async with target_db() as db, db.begin():
        run = await service.start(db, target_doc_id, rules=source["rules"], revision_id=revision_id,
            request_id=f"G1-page-batch:{source_run_id}")
        run.extraction_profile = {**run.extraction_profile, "extraction_strategy": "page_batch_v1",
            "coordinator_field_key": min(rule.field_key for rule in source["rules"]),
            "max_run_seconds": MAX_RUN_SECONDS, "page_batch_chars": settings.agent.page_batch_chars}
        run_id = run.parse_run_id
    model = settings.models["vision_llm"]
    circuit = ServiceCircuit(MemoryCircuitStore())
    ledger = CallLedger(target_db)
    gateway = ModelGateway(model_client, ledger, circuit, GatewayPolicy())
    executor = ReliablePageExecutor(target_db, gateway, ReliableFieldExecutorConfig(
        provider_key=service_key("model", model.base_url, model.model_name, model.model_version),
        model=model.model_name, thinking_enabled=settings.agent.thinking_enabled), image_provider=image_provider)
    result = None
    error = None
    last_progress = 0.0
    try:
        async with asyncio.timeout(max(1, MAX_RUN_SECONDS - (time.monotonic() - started))):
            while True:
                await _requeue_due(target_db, run_id)
                result = await service.process_run(target_db, run_id, executor=executor,
                    writer=executor.write_result, before_publish=executor.before_publish,
                    owner=f"G1-compare:{source_run_id}", max_quantums=3, lease_seconds=300)
                progress = await _run_progress(target_db, run_id)
                if time.monotonic() - last_progress >= 15:
                    last_progress = time.monotonic()
                    print(f"[G1] {source_run_id} status={progress['status']} "
                          f"coordinator={progress['coordinator_state']} progress={progress['page_progress']} "
                          f"error={progress['last_error']}", flush=True)
                if progress["status"] in {"COMPLETED", "COMPLETED_WITH_REVIEW", "FAILED", "CANCELLED", "SUPERSEDED", "PAUSED"}:
                    if progress["status"] == "FAILED":
                        error = progress["last_error"] or "isolated_run_failed"
                    break
                if progress["coordinator_state"] in {"waiting_service", "waiting_external_quota"}:
                    if progress["retry_seconds"] is None:
                        raise RuntimeError(f"{progress['coordinator_state']} requires external action: {progress['last_error']}")
                    await asyncio.sleep(min(30.0, max(0.5, progress["retry_seconds"])))
                elif not result.get("quantums_executed"):
                    # Avoid a busy loop while a lease or delayed yield is pending.
                    await asyncio.sleep(min(30.0, max(0.5, progress["retry_seconds"] or 1)))
    except Exception as exc:
        error = "runner_deadline_exceeded" if isinstance(exc, TimeoutError) else f"{type(exc).__name__}: {exc}"
        print(f"[G1] {source_run_id} failed: {error}", flush=True)
        # Only isolated records are closed; cancelled provider calls may retain
        # uncertain charges, which are exposed in the report rather than hidden.
        async with target_db() as db, db.begin():
            failed_run = await db.get(ParseRun, run_id)
            failed_run.status, failed_run.error_message = "FAILED", error
            jobs = list((await db.execute(select(ParseFieldJob).where(ParseFieldJob.run_id == run_id))).scalars())
            for job in jobs:
                if job.execution_state not in {"done", "cancelled", "superseded"}:
                    job.execution_state, job.last_error = "failed_terminal", error
                    job.lease_owner = job.lease_until = job.next_attempt_at = None
    finally:
        async with target_db() as db:
            execution = await service.execution(db, run_id, limit=500)
            calls = list((await db.execute(select(ParseModelCall).where(
                ParseModelCall.job_id.in_(select(ParseFieldJob.job_id).where(ParseFieldJob.run_id == run_id))))).scalars())
            fields = list((await db.execute(select(ExtractedFieldModel).where(
                ExtractedFieldModel.parse_run_id == run_id))).scalars())
        report = {"source_run_id": str(source_run_id), "source_file_hash": source["document"].file_hash,
            "isolated_run_id": str(run_id), "elapsed": round(time.monotonic() - started, 3),
            "modelcalls": len(calls), "tokens": sum(int((call.usage or {}).get("total_tokens") or 0) for call in calls),
            "status": execution.get("status"), "page_progress": execution.get("page_progress"),
            "error": error, "accuracy_status": "UNVERIFIED_NO_HUMAN_GOLD",
            "uncertain_charge_calls": sum(call.charge_state in {"reserved", "uncertain"} for call in calls),
            "call_errors": [{"call_id": str(call.call_id), "state": call.state,
                "error": ((call.reservation or {}).get("gateway_outcome") or {}).get("error"),
                "wait_state": ((call.reservation or {}).get("gateway_outcome") or {}).get("wait_state")}
                for call in calls if call.state not in {"succeeded", "queued"}],
            "fields": {field.field_key: {"field_value": field.field_value, "facts": field.instances or [],
                                         "risks": (field.extraction_detail or {}).get("field_completion", {}),
                                         "validation_status": field.validation_status}
                       for field in fields}, "result": result}
        report_path = out_dir / f"{source_run_id}.json"
        await asyncio.to_thread(report_path.write_text, json.dumps(report, ensure_ascii=False, indent=2, default=str), "utf-8")
    return report


async def _requeue_due(target_db, run_id: UUID) -> int:
    """Mirror due wakeup recovery, scoped strictly to this isolated run."""
    now = datetime.utcnow()
    async with target_db() as db, db.begin():
        run = await db.get(ParseRun, run_id, with_for_update=True)
        if run.status in {"CANCELLED", "SUPERSEDED", "PAUSED", "FAILED", "COMPLETED", "COMPLETED_WITH_REVIEW"}:
            return 0
        jobs = list((await db.execute(select(ParseFieldJob).where(
            ParseFieldJob.run_id == run_id,
            ParseFieldJob.execution_state.in_(["waiting_service", "waiting_external_quota"]),
            ParseFieldJob.next_attempt_at.is_not(None), ParseFieldJob.next_attempt_at <= now)
            .with_for_update())).scalars())
        repo = JobRepository(db)
        for job in jobs:
            job.execution_state = "queued"
            job.fence_token += 1
            job.next_attempt_at = job.lease_owner = job.lease_until = None
            job.updated_at = now
            await repo.enqueue_outbox(kind="field_job_wakeup", aggregate_id=job.job_id,
                dedupe_key=f"{job.job_id}:compare:{job.progress_seq}",
                payload={"job_id": str(job.job_id), "run_id": str(run_id)})
        return len(jobs)


async def _run_progress(target_db, run_id: UUID) -> dict:
    async with target_db() as db:
        execution = await ReliableExtractionService().execution(db, run_id, limit=500)
        run = await db.get(ParseRun, run_id)
        jobs = list((await db.execute(select(ParseFieldJob).where(ParseFieldJob.run_id == run_id))).scalars())
        coordinator_key = (run.extraction_profile or {}).get("coordinator_field_key")
        coordinator = next(job for job in jobs if job.field_key == coordinator_key)
        retry_seconds = ((coordinator.next_attempt_at - datetime.utcnow()).total_seconds()
                         if coordinator.next_attempt_at else None)
        return {"status": execution.get("status"), "coordinator_state": coordinator.execution_state,
                "retry_seconds": retry_seconds, "last_error": coordinator.last_error,
                "page_progress": execution.get("page_progress")}


async def main(args):
    if args.test_database != TARGET_DATABASE:
        raise SystemExit(f"refusing non-G1 test database: {args.test_database!r}")
    out_dir = args.out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    if settings.database.name == args.test_database:
        raise SystemExit("source and test databases must differ")
    source_engine = create_async_engine(settings.database.url, poolclass=NullPool,
        connect_args={"server_settings": {"default_transaction_read_only": "on"}})
    target_engine = create_async_engine(_url_for(args.test_database), poolclass=NullPool)
    source_db = async_sessionmaker(source_engine, expire_on_commit=False)
    target_db = async_sessionmaker(target_engine, expire_on_commit=False)
    model = settings.models["vision_llm"]
    http_client = httpx.AsyncClient(trust_env=False) if args.no_proxy else None
    print(f"[G1] model host={httpx.URL(model.base_url).host} "
          f"proxy={'disabled' if args.no_proxy else 'environment'}", flush=True)
    client_kwargs = {"base_url": LLMStrategy._normalize_base(model.base_url),
                     "api_key": model.api_key or "dummy", "timeout": model.timeout, "max_retries": 0}
    if http_client is not None:
        client_kwargs["http_client"] = http_client
    client = AsyncOpenAI(**client_kwargs)
    try:
        reports = []
        for source_run_id in args.source_run_id:
            reports.append(await _run_one(source_db, target_db, UUID(source_run_id), out_dir, client))
        (out_dir / "summary.json").write_text(json.dumps(reports, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    finally:
        await client.close()
        if http_client is not None:
            await http_client.aclose()
        await source_engine.dispose()
        await target_engine.dispose()


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run-id", action="append", required=True,
                        help="Frozen source ParseRun UUID; repeat for each comparison document")
    parser.add_argument("--test-database", default=TARGET_DATABASE)
    parser.add_argument("--out-dir", type=Path, default=Path("output/page-batch-20261013"))
    parser.add_argument("--no-proxy", action="store_true",
                        help="Disable environment HTTP(S) proxies for the model client")
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(main(parse_args()))
