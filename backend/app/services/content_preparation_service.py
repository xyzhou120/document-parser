"""初始文档内容准备编排（P13 工作进程边界）。

本服务为冻结的请求构建一个可复用 Artifact，并通过 P11 发布一次。
字段任务不会独立调用 PDF 解析或 OCR。
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import os
import struct
import tempfile
from collections.abc import Callable
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.content import ContentBuildJob
from app.models.database import Document, DocumentArtifact, ParseRun
from app.parsers.ocr_client import PaddleOCRClient
from app.parsers.pdf_parser import PDFParser
from app.rules.semantic_contract import rule_semantic_hash
from app.services.artifact_service import preprocessing_profile
from app.services.assistant.index_repository import digest
from app.services.content_build_service import ContentBuildService, ServiceUnavailable
from app.services.content_publish_service import ContentPublishService
from app.services.execution.job_repository import JobRepository
from app.services.ocr_capacity import RedisOCRCapacity
from app.services.service_circuit import RedisCircuitStore, ServiceCircuit, service_key
from app.services.vision_transcription import VisionPageTranscriber
from app.storage.minio_client import minio_client

CONTENT_BUILD_OUTBOX = "content.build.execute"


class ContentPreparationError(RuntimeError):
    pass


class ContentPreparationService:
    def __init__(self, *, parser: PDFParser | None = None, builder: ContentBuildService | None = None,
                 publisher: ContentPublishService | None = None, ocr: PaddleOCRClient | None = None,
                 cache_dir: str | Path | None = None):
        self.parser = parser or PDFParser()
        self.builder = builder or ContentBuildService()
        self.publisher = publisher or ContentPublishService()
        self.ocr = ocr
        self.cache_dir = Path(cache_dir) if cache_dir else Path(tempfile.gettempdir()) / "document-parser-content-v2"

    async def prepare_build(self, db: AsyncSession, document_id: UUID, *, profile: dict[str, Any],
                            request_id: str, parent_revision_id: UUID | None = None) -> dict[str, Any]:
        document = await db.get(Document, document_id, with_for_update=True)
        if document is None:
            raise KeyError("文档不存在")
        if not document.file_hash:
            raise ContentPreparationError("文档缺少文件hash")
        frozen = deepcopy(profile)
        frozen["request_id"] = request_id
        frozen["document_file_hash"] = document.file_hash
        frozen.setdefault("preprocess_profile", preprocessing_profile("checklist_agent"))
        frozen.setdefault("render_dpi", 150)
        existing = await db.scalar(select(ContentBuildJob).where(
            ContentBuildJob.document_id == document_id,
            ContentBuildJob.file_hash == document.file_hash,
            ContentBuildJob.profile["request_id"].as_string() == request_id).limit(1))
        if existing:
            if digest(existing.profile) != digest(frozen):
                raise ContentPreparationError("request_id已绑定不同构建配置或规则快照")
            return self.serialize(existing)
        job = await self.builder.create_job(db, document_id, document.file_hash, frozen,
                                            parent_revision_id=parent_revision_id)
        await JobRepository(db).enqueue_outbox(kind=CONTENT_BUILD_OUTBOX, aggregate_id=job.build_id,
            dedupe_key=f"content-build:{document_id}:{request_id}",
            payload={"build_id": str(job.build_id), "document_id": str(document_id), "file_hash": document.file_hash})
        await db.flush()
        return self.serialize(job)

    @staticmethod
    async def _resolve(resolver: Callable[[Document], Any] | None, document: Document) -> bytes:
        if resolver is None:
            data = await asyncio.to_thread(minio_client.download, document.file_path)
        else:
            data = resolver(document)
            if inspect.isawaitable(data):
                data = await data
        if isinstance(data, bytes | bytearray | memoryview):
            return bytes(data)
        if isinstance(data, str | Path):
            return await asyncio.to_thread(Path(data).read_bytes)
        raise ContentPreparationError("source resolver must return PDF bytes or a file path")

    @staticmethod
    def _pages_with_checkpoint(pages: list[dict], checkpoint: dict) -> list[dict]:
        pages = deepcopy(pages)
        for page in pages:
            number = int(page["page_number"])
            cp = checkpoint.get("pages", {}).get(str(number), {})
            page_regions = []
            for rid in cp.get("regions", []):
                item = deepcopy(checkpoint.get("regions", {}).get(f"p{number}:{rid}", {}))
                page_regions.append(item)
            ocr_blocks = []
            for region in page_regions:
                if region.get("state") != "processed":
                    continue
                for ordinal, block in enumerate(region.get("blocks") or []):
                    locator = deepcopy(block.get("locator"))
                    bbox = deepcopy(block.get("bbox"))
                    ocr_blocks.append({"text": block.get("raw_text", ""), "raw_ocr": block.get("raw_text", ""),
                                      "normalized_text": block.get("normalized_text", block.get("raw_text", "")),
                                      "bbox": bbox, "source": "automatic_ocr", "region_id": region.get("region_id"),
                                      "block_id": f"{region.get('region_id')}:block:{ordinal}",
                                      "original_locator": locator, "image_hash": region.get("image_hash"),
                                      "coordinate_system": block.get("coordinate_system") or region.get("coordinate_system"),
                                      "confidence": block.get("confidence"), "label": block.get("label")})
                if not region.get("blocks") and region.get("raw_ocr") and region.get("source") != "native":
                    ocr_blocks.append({"text": region["raw_ocr"], "raw_ocr": region["raw_ocr"], "source": "automatic_ocr",
                                       "block_id": f"p{number}:{region.get('region_id')}:block:0", "region_id": region.get("region_id"),
                                       "image_hash": region.get("image_hash"), "bbox": region.get("bbox"),
                                       "coordinate_system": region.get("coordinate_system"), "location_precision": "PAGE"})
            if ocr_blocks:
                page["ocr_blocks"] = ocr_blocks
                native = str(page.get("native_text", page.get("text", "")) or "")
                offset = len(native) + 1
                for item in ocr_blocks:
                    item["start_offset"], item["end_offset"] = offset, offset + len(item["text"])
                    offset += len(item["text"]) + 1
                page["text"] = native + "\n" + "\n".join(item["text"] for item in ocr_blocks)
                page["text_blocks"] = [*deepcopy(page.get("text_blocks") or []), *ocr_blocks]
            page["coverage_manifest"] = {"page_number": number, "scope_complete": cp.get("scope_attested") is True,
                                          "regions": page_regions}
        return pages

    @staticmethod
    def _page_inputs(pages: list[dict], inspections: list[Any]) -> list[dict]:
        inspection_map = {item.page_number: item for item in inspections}
        inputs = []
        for page in pages:
            item = deepcopy(page)
            inspection = inspection_map.get(page["page_number"])
            if inspection:
                item["needs_ocr"] = inspection.needs_ocr
                item["is_scan"] = inspection.is_scan
                native_safe = (not inspection.needs_ocr and not page.get("image_count") and not page.get("rotation")
                               and bool(str(page.get("native_text", page.get("text", "")) or "").strip())
                               and not page.get("native_layer_suspect"))
                item["inspection"] = {"native_readability": "verified" if native_safe else "suspect", "scope_complete": native_safe}
                item["native_coverage_complete"] = native_safe
            inputs.append(item)
        return inputs

    async def run_build(self, sessions, build_id: UUID, *, source_resolver: Callable[[Document], Any] | None = None,
                        renderer: Callable | None = None, ocr: Callable | None = None,
                        worker_id: str = "content-preparer", max_regions: int = 20) -> dict[str, Any]:
        async with sessions() as db:
            job = await db.get(ContentBuildJob, build_id)
            if job is None:
                raise KeyError("content build不存在")
            document = await db.get(Document, job.document_id)
            if document is None or document.file_hash != job.file_hash:
                raise ContentPreparationError("构建文件版本已变化")
            if job.phase == "published" and (job.checkpoint or {}).get("published_revision_id"):
                return {"build": self.serialize(job), "status": "published", "revision_id": job.checkpoint["published_revision_id"],
                        "run_id": job.checkpoint.get("parse_run_id")}
            frozen_profile = deepcopy(job.profile)
            baseline = deepcopy((job.checkpoint or {}).get("source_pages"))
            source_hash = job.file_hash
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        pdf_cache = self.cache_dir / f"{source_hash}.pdf"
        if not pdf_cache.exists() or hashlib.sha256(await asyncio.to_thread(pdf_cache.read_bytes)).hexdigest() != source_hash:
            source = await self._resolve(source_resolver, document)
            if hashlib.sha256(source).hexdigest() != source_hash:
                raise ContentPreparationError("PDF原件hash与冻结文件版本不一致")
            temporary = self.cache_dir / f"{source_hash}-{os.getpid()}-{build_id}.part"
            await asyncio.to_thread(temporary.write_bytes, source)
            await asyncio.to_thread(os.replace, temporary, pdf_cache)
        pdf_path = str(pdf_cache)
        try:
            if baseline is None:
                pages = await asyncio.to_thread(self.parser.extract_pages, pdf_path)
                inspections = await asyncio.to_thread(self.parser.inspect_pages, pdf_path)
                page_inputs = self._page_inputs(pages, inspections)
                async with sessions() as db, db.begin():
                    current = await db.get(ContentBuildJob, build_id, with_for_update=True, populate_existing=True)
                    cp = deepcopy(current.checkpoint or {})
                    cp.setdefault("source_pages", page_inputs)
                    current.checkpoint = cp
                    page_inputs = deepcopy(cp["source_pages"])
            else:
                page_inputs = baseline
            render = renderer
            ocr_call = ocr
            vision = None
            if render is None:
                async def render(page, region):
                    image = await asyncio.to_thread(self.parser.get_page_image, pdf_path, int(page["page_number"]))
                    width, height = struct.unpack(">II", image[16:24])
                    return {"image_bytes": image, "scope_complete": True, "whole_page": True,
                            "image_width": width, "image_height": height,
                            "image_ref": {"source": "document_pdf", "page_number": page["page_number"],
                                          "file_hash": source_hash, "image_hash": hashlib.sha256(image).hexdigest()}}
            if ocr_call is None:
                if self.ocr is None and preprocessing_profile("checklist_agent")["version"] != frozen_profile["preprocess_profile"]["version"]:
                    raise ContentPreparationError("冻结OCR部署配置已变化，不能混用识别版本")
                client = self.ocr or PaddleOCRClient()
                ocr_limit = int(settings.models["ocr"].max_concurrency)
                transcriber = VisionPageTranscriber()

                async def ocr_call(image, page_number, region):
                    gate = RedisOCRCapacity(redis) if redis is not None else None
                    token = None
                    if gate is not None:
                        token = await gate.try_acquire(ocr_limit, int(client.timeout) + 30)
                        if token is None:
                            return {"ocr_outcome": "capacity"}
                    try:
                        outcome = await client.recognize_regions_outcome(
                            image, page_number, int(region.get("image_width") or 1), int(region.get("image_height") or 1))
                    finally:
                        if gate is not None:
                            await gate.release(token)
                    if outcome.status == "unavailable":
                        raise RuntimeError(outcome.error or "ocr_unavailable")
                    if outcome.status == "empty":
                        return {"ocr_outcome": "empty"}
                    return outcome.blocks

                async def vision(image, page_number, region):
                    return await transcriber.transcribe(image, page_number, region)
            redis = None
            if self.builder.circuit is None:
                from redis.asyncio import Redis
                redis = Redis.from_url(settings.redis.url)
                self.builder.circuit = ServiceCircuit(RedisCircuitStore(redis))
            async with sessions() as db:
                job = await db.get(ContentBuildJob, build_id, with_for_update=True)
                try:
                    checkpoint = await self.builder.process(db, job, page_inputs, renderer=render, ocr=ocr_call,
                        vision=vision,
                        service_key_value=service_key("ocr", str(frozen_profile["preprocess_profile"]["manifest"].get("ocr_service") or "injected"),
                                                      str(frozen_profile["preprocess_profile"]["manifest"].get("ocr_model") or "ocr"),
                                                      frozen_profile["preprocess_profile"]["version"]),
                        worker_id=worker_id, expected_page_count=len(page_inputs), max_regions=max_regions)
                except ServiceUnavailable:
                    job = await db.get(ContentBuildJob, build_id, populate_existing=True)
                    checkpoint = job.checkpoint
                if job.dispatch_state != "succeeded":
                    if job.dispatch_state in {"yielded", "queued", "waiting_service"}:
                        event = await JobRepository(db).enqueue_outbox(kind=CONTENT_BUILD_OUTBOX, aggregate_id=job.build_id,
                            dedupe_key=f"content-build:{job.build_id}:attempt:{job.attempt}:fence:{job.fence_token}",
                            payload={"build_id": str(job.build_id), "document_id": str(job.document_id), "file_hash": job.file_hash})
                        if checkpoint.get("retry_at") and job.dispatch_state == "waiting_service":
                            event.next_attempt_at = datetime.fromisoformat(checkpoint["retry_at"])
                    await db.commit()
                    return {"build": self.serialize(job), "status": job.dispatch_state, "coverage_manifest": checkpoint.get("coverage_manifest")}
            async with sessions() as db, db.begin():
                job = await db.get(ContentBuildJob, build_id, with_for_update=True)
                if job.phase == "published":
                    return {"build": self.serialize(job), "status": "published", "revision_id": job.checkpoint["published_revision_id"],
                            "run_id": job.checkpoint.get("parse_run_id")}
                artifact_pages = self._pages_with_checkpoint(page_inputs, checkpoint)
                quality = {"page_count": len(artifact_pages), "coverage_manifest": checkpoint["coverage_manifest"],
                           "failed_pages": [p["page_number"] for p in checkpoint["coverage_manifest"]["pages"] if not p["scope_complete"]],
                           "preprocess_manifest": job.profile.get("preprocess_profile", {}).get("manifest", {}),
                           "source_review_status": "UNVERIFIED"}
                artifact = DocumentArtifact(document_id=document.document_id, file_hash=document.file_hash,
                    preprocess_profile_version=job.profile.get("preprocess_profile", {}).get("version", "content-build-v1"),
                    status="READY", pages=artifact_pages, quality=quality)
                db.add(artifact)
                await db.flush()
                published = await self.publisher.publish_artifact(db, artifact,
                    expected_parent_revision_id=job.parent_revision_id,
                    expected_cas_version=(await self.publisher._head(db, document.document_id, lock=True)).cas_version,
                    expected_invalidation_watermark=(await self.publisher._head(db, document.document_id, lock=True)).invalidation_watermark,
                    origin_event_id=f"content-build:{job.build_id}", request_id=job.profile.get("request_id"), source_run_id=None,
                    source_kind="automatic_ocr", change_kind="initial_content", suppress_successor=True)
                revision = published["revision"]
                rules = deepcopy(job.profile.get("rules") or [])
                run = ParseRun(parse_run_id=uuid5(NAMESPACE_URL, f"content-build-run:{job.build_id}"),
                               document_id=document.document_id, artifact_id=artifact.artifact_id,
                               parent_parse_run_id=UUID(str(job.profile.get("parent_parse_run_id") or job.profile.get("parent_run_id")))
                               if job.profile.get("parent_parse_run_id") or job.profile.get("parent_run_id") else None,
                               content_revision_id=revision.revision_id, contract_version="reliable-extraction-v2",
                               execution_generation=0, requested_field_keys=None, run_mode="FULL",
                               checklist_type=document.checklist_type,
                               extraction_profile={**deepcopy(job.profile), "content_revision_id": str(revision.revision_id),
                                                    "invalidation_watermark": int(published["head"].invalidation_watermark or 0),
                                                    "source_recompute_policy": "FULL"}, status="PLANNED", cache_status="ARTIFACT_MISS")
                db.add(run)
                await db.flush()
                fields = []
                for rule in rules:
                    item = deepcopy(rule)
                    item.setdefault("field_key", item.get("key"))
                    if not item.get("field_key"):
                        raise ContentPreparationError("冻结规则缺少field_key")
                    item.setdefault("rule_hash", rule_semantic_hash(item))
                    item.setdefault("snapshot", deepcopy(rule))
                    fields.append(item)
                jobs = await JobRepository(db).initialize_jobs(run.parse_run_id, revision_id=revision.revision_id, fields=fields)
                for field_job in jobs:
                    await JobRepository(db).enqueue_outbox(kind="field_job_wakeup", aggregate_id=field_job.job_id,
                        dedupe_key=f"content-build-run:{run.parse_run_id}:{field_job.field_key}",
                        payload={"run_id": str(run.parse_run_id), "job_id": str(field_job.job_id), "revision_id": str(revision.revision_id)})
                job.phase, job.dispatch_state, job.checkpoint = "published", "succeeded", checkpoint
                checkpoint["published_revision_id"] = str(revision.revision_id)
                checkpoint["parse_run_id"] = str(run.parse_run_id)
                job.checkpoint = deepcopy(checkpoint)
                await db.flush()
                return {"build": self.serialize(job), "status": "published", "revision_id": str(published["revision"].revision_id),
                        "run_id": str(run.parse_run_id), "successor_run_id": str(published["successor"].parse_run_id) if published.get("successor") else None}
        finally:
            if 'redis' in locals() and redis is not None:
                self.builder.circuit = None
                await redis.aclose()

    @staticmethod
    def serialize(job):
        return {"build_id": str(job.build_id), "document_id": str(job.document_id), "file_hash": job.file_hash,
                "phase": job.phase, "dispatch_state": job.dispatch_state, "checkpoint": deepcopy(job.checkpoint or {}),
                "attempt": job.attempt, "fence_token": job.fence_token, "last_error": job.last_error}


content_preparation_service = ContentPreparationService()
