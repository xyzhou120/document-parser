"""可恢复的 OCR 与内容准备。

P09 在不可变构建检查点停止。发布 Artifact、内容修订、影响记录和处理头
由 P11 负责。因此本服务只把页面和区域工作转为确定性的检查点及覆盖清单。
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import func, select, text

from app.models.content import ContentBuildJob
from app.services.content_build_storage import ContentCheckpoint, load_checkpoint, prepare_storage, store_checkpoint
from app.services.service_circuit import CircuitPermit, ServiceCircuit


logger = logging.getLogger(__name__)


def _prepare_checkpoint(checkpoint):
    started = time.monotonic()
    value = _jsonable(checkpoint)
    size = len(json.dumps(value).encode("utf-8"))
    return value, size, time.monotonic() - started


def _digest(value: bytes | str) -> str:
    return hashlib.sha256(value if isinstance(value, bytes) else value.encode("utf-8")).hexdigest()


def _normalise(value: Any) -> str:
    return " ".join(str(value or "").replace("\u3000", " ").split())


def _jsonable(value: Any) -> Any:
    """检查点只保存 JSON。OCR 块是 TextBlock，不能原样写入。"""
    if hasattr(value, "model_dump"):
        return _jsonable(value.model_dump(mode="json"))
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def image_hash(image: bytes | bytearray | memoryview) -> str:
    return _digest(bytes(image))


def region_key(file_hash: str, page_number: int, region_id: str, image_digest: str, profile: Mapping[str, Any]) -> str:
    profile_id = _digest(json.dumps(dict(profile), sort_keys=True, separators=(",", ":")))
    return _digest(f"{file_hash}:{page_number}:{region_id}:{image_digest}:{profile_id}")


@dataclass(frozen=True)
class OCRRegionResult:
    region_id: str
    page_number: int
    state: str
    raw_ocr: str = ""
    normalized_ocr: str = ""
    confidence: float | None = None
    bbox: list[float] | None = None
    coordinate_system: str | None = None
    image_hash: str | None = None
    error: str | None = None
    blocks: list[dict[str, Any]] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "region_id": self.region_id, "page_number": self.page_number, "state": self.state,
            "raw_ocr": self.raw_ocr, "normalized_ocr": self.normalized_ocr,
            "confidence": self.confidence, "bbox": self.bbox,
            "coordinate_system": self.coordinate_system, "image_hash": self.image_hash,
            "error": self.error,
            "blocks": deepcopy(self.blocks or []),
        }


class ContentBuildError(RuntimeError):
    pass


class ServiceUnavailable(ContentBuildError):
    def __init__(self, message: str, *, retry_at: datetime | None = None):
        super().__init__(message)
        self.retry_at = retry_at


class ContentBuildService:
    """构建页面与区域检查点，但不提交发布状态。"""

    def __init__(self, *, circuit: ServiceCircuit | None = None, max_region_attempts: int = 3,
                 lease_seconds: int = 300, ocr_parallelism: int = 2):
        self.ocr_parallelism = ocr_parallelism
        self.circuit = circuit
        self.max_region_attempts = max(1, max_region_attempts)
        self.lease_seconds = lease_seconds

    async def create_job(
        self, db: AsyncSession, document_id: UUID, file_hash: str, profile: Mapping[str, Any],
        *, parent_revision_id: UUID | None = None,
    ) -> ContentBuildJob:
        job = ContentBuildJob(document_id=document_id, file_hash=file_hash, profile=deepcopy(dict(profile)),
                              parent_revision_id=parent_revision_id, phase="planned", dispatch_state="queued", attempt=0, fence_token=0,
                              checkpoint={"pages": {}, "regions": {}})
        db.add(job)
        await db.flush()
        return job

    @staticmethod
    def _regions(page: Mapping[str, Any], page_number: int) -> list[dict[str, Any]]:
        explicit = list(page.get("regions") or page.get("ocr_regions") or [])
        if explicit:
            result = []
            for i, item in enumerate(explicit):
                item = dict(item)
                result.append({**item, "region_id": str(item.get("region_id") or item.get("id") or f"p{page_number}:r{i}")})
            return result
        reliable_native = page.get("native_coverage_complete") is True
        # 没有图像且来源未标记扫描时，PDFParser 可证明纯文本页属于原生文本。
        # 含图像页面需要明确的渲染或 OCR 区域；空默认值不能伪造完整覆盖。
        inspection = page.get("inspection") or {}
        reliable_native = reliable_native or (
            inspection.get("native_readability") == "verified"
            and inspection.get("scope_complete") is True
            and not page.get("native_layer_suspect")
        )
        return [{"region_id": f"p{page_number}:page", "kind": "native" if reliable_native else "ocr", "whole_page": True}]

    @staticmethod
    def _page_image(page: Mapping[str, Any], region: Mapping[str, Any]) -> bytes | None:
        data = region.get("image_bytes") or page.get("image_bytes") or page.get("rendered_image")
        return bytes(data) if data is not None else None

    async def _prepare_partner_ocr(self, pages, page_index, region_index, checkpoint, renderer):
        """准备下一张需要 OCR 的页。文字层页面不占 OCR 名额。"""
        if self.ocr_parallelism < 2:
            return None
        seen = False
        for pi, page in enumerate(pages):
            number = int(page.get("page_number", page.get("page", 0)))
            if number <= 0:
                continue
            regions = self._regions(page, number)
            for ri, region in enumerate(regions):
                if not seen:
                    if pi == page_index and ri == region_index:
                        seen = True
                    continue
                rid = str(region["region_id"])
                key = f"p{number}:{rid}"
                if key in (checkpoint.get("prefetched_ocr") or {}):
                    continue
                if region.get("kind") == "native" or region.get("source") == "native":
                    continue
                cached = (checkpoint.get("regions") or {}).get(key) or {}
                if cached.get("state") in {"processed", "blank", "unreadable", "failed"}:
                    continue
                image = self._page_image(page, region)
                if image is None and renderer is not None:
                    try:
                        rendered = await self._call(renderer, page, region)
                    except Exception:
                        return None
                    if isinstance(rendered, Mapping):
                        image = rendered.get("image_bytes")
                        region = {**region, **{k: v for k, v in rendered.items() if k != "image_bytes"}}
                    else:
                        image = rendered
                if image is None:
                    continue
                self._ocr_images[key] = {
                    "image_bytes": image,
                    "updates": {k: v for k, v in region.items() if k != "image_bytes"},
                }
                return key, image, number, region
        return None

    async def _call(self, fn: Callable[..., Any], *args: Any) -> Any:
        value = fn(*args)
        return await value if inspect.isawaitable(value) else value

    @staticmethod
    def _locate_blocks(values, region, page):
        for block in values.get("blocks") or []:
            locator = block.get("locator") or {}
            box = locator.get("bbox") or block.get("bbox")
            if isinstance(box, Mapping):
                box = [box.get(k) for k in ("x0", "y0", "x1", "y1")]
            block["original_locator"] = deepcopy(locator or None)
            system = locator.get("coordinate_system") or block.get("coordinate_system") or region.get("coordinate_system")
            if box and system == "PIXEL" and region.get("image_width") and region.get("image_height"):
                target = region.get("bbox") or [0, 0, page.get("width"), page.get("height")]
                if all(v is not None for v in target):
                    x0, y0, x1, y1 = target
                    box = [x0 + box[0] * (x1-x0)/region["image_width"], y0 + box[1] * (y1-y0)/region["image_height"],
                           x0 + box[2] * (x1-x0)/region["image_width"], y0 + box[3] * (y1-y0)/region["image_height"]]
                    system = "PDF_POINT"
            block["bbox"] = box
            block["coordinate_system"] = system
            block["location_precision"] = "REGION" if box else "PAGE"
        values["location_precision"] = "REGION" if values.get("bbox") else "PAGE"
        values["image_ref"] = deepcopy(region.get("image_ref"))

    @staticmethod
    def _defer(checkpoint, reason: str, expected_count: int):
        checkpoint["retry_at"] = (datetime.utcnow() + timedelta(seconds=5)).isoformat()
        checkpoint["last_wait_reason"] = reason
        checkpoint["coverage_manifest"] = ContentBuildService._manifest(checkpoint, expected_count)

    async def _save(self, db, job, checkpoint, fence, worker_id, *, state="running", phase="regions", error=None):
        # 在持行锁前准备 JSON；独立续租心跳仍可运行。ORM 驱动的实际
        # JSON 编码和数据库提交耗时归入 write_seconds。
        serialization_started = time.monotonic()
        storage_plan = await asyncio.to_thread(prepare_storage, job.build_id, checkpoint, _jsonable)
        checkpoint_bytes = len(json.dumps(storage_plan[0]).encode("utf-8"))
        serialization_seconds = time.monotonic() - serialization_started
        lock_started = time.monotonic()
        current = await db.get(ContentBuildJob, job.build_id, with_for_update=True, populate_existing=True)
        lock_acquire_seconds = time.monotonic() - lock_started
        now = datetime.utcnow()
        if (current is None or current.fence_token != fence or current.lease_owner != worker_id
                or current.lease_until is None or current.lease_until <= now):
            logger.warning("content_build_save_stale build_id=%s fence=%s current_fence=%s "
                           "lease_until=%s observed_at=%s serialization_seconds=%.3f "
                           "lock_acquire_seconds=%.3f checkpoint_bytes=%s",
                           job.build_id, fence, getattr(current, "fence_token", None),
                           getattr(current, "lease_until", None), now.isoformat(), serialization_seconds,
                           lock_acquire_seconds, checkpoint_bytes)
            await db.rollback()
            raise ContentBuildError("stale content build worker cannot submit")
        lease_remaining_seconds = (current.lease_until - now).total_seconds()
        write_started = time.monotonic()
        prepared, fingerprints, changed_entries = await store_checkpoint(db, current, storage_plan)
        current.checkpoint = prepared
        current.dispatch_state = state
        current.phase = phase
        current.last_error = error
        current.lease_until = datetime.utcnow() + timedelta(seconds=self.lease_seconds) if state == "running" else None
        await db.flush()
        await db.commit()
        if isinstance(checkpoint, ContentCheckpoint):
            checkpoint.fingerprints = fingerprints
            checkpoint["source_pages_stored"] = prepared["source_pages_stored"]
        logger.info("content_build_save build_id=%s fence=%s state=%s serialization_seconds=%.3f "
                    "lock_acquire_seconds=%.3f write_seconds=%.3f checkpoint_bytes=%s lease_remaining_seconds=%.3f changed_entries=%s",
                    job.build_id, fence, state, serialization_seconds, lock_acquire_seconds,
                    time.monotonic() - write_started, checkpoint_bytes, lease_remaining_seconds, changed_entries)

    @staticmethod
    def _manifest(checkpoint, expected_count):
        pages = []
        for number in range(1, expected_count + 1):
            cp = checkpoint["pages"].get(str(number), {})
            regions = [deepcopy(checkpoint["regions"].get(f"p{number}:{rid}", {"region_id": rid, "state": "pending"})) for rid in cp.get("regions", [])]
            closed = bool(regions) and cp.get("scope_attested") is True and all(r["state"] in {"processed", "blank"} for r in regions)
            pages.append({"page_number": number, "scope_complete": closed, "regions": regions})
        return {"expected_page_count": expected_count, "pages": pages}

    async def process(
        self,
        db: AsyncSession,
        job: ContentBuildJob,
        pages: list[Mapping[str, Any]],
        *,
        renderer: Callable[[Mapping[str, Any], Mapping[str, Any]], bytes | Awaitable[bytes]] | None = None,
        ocr: Callable[[bytes, int, Mapping[str, Any]], Any] | None = None,
        vision: Callable[[bytes, int, Mapping[str, Any]], Any] | None = None,
        service_key_value: str = "ocr:content-build",
        worker_id: str = "content-builder",
        expected_page_count: int | None = None,
        max_regions: int = 20,
        max_seconds: float | None = None,
    ) -> dict[str, Any]:
        current = await db.get(ContentBuildJob, job.build_id, with_for_update=True, populate_existing=True)
        if current is None:
            raise ContentBuildError("content build does not exist")
        if current.lease_until and current.lease_until > datetime.utcnow():
            await db.rollback()
            raise ContentBuildError("content build is already leased")
        from app.config import settings
        if settings.agent.reliable_quantum_queue_enabled:
            from app.models.execution import ParseFieldJob
            await db.execute(text('SELECT pg_advisory_xact_lock(753101503)'))
            now = datetime.utcnow()
            running = await db.scalar(select(func.count()).select_from(ParseFieldJob).where(
                ParseFieldJob.execution_state == "running", ParseFieldJob.lease_until > now))
            running += await db.scalar(select(func.count()).select_from(ContentBuildJob).where(
                ContentBuildJob.dispatch_state == "running", ContentBuildJob.lease_until > now))
            if running >= settings.attachment_import.parse_concurrency:
                current.dispatch_state = "yielded"
                await db.commit()
                return deepcopy(current.checkpoint or {})
        previous_fence, previous_lease_until = current.fence_token, current.lease_until
        current.fence_token = (current.fence_token or 0) + 1
        current.lease_owner = worker_id
        current.lease_until = datetime.utcnow() + timedelta(seconds=self.lease_seconds)
        current.attempt = (current.attempt or 0) + 1
        fence = current.fence_token
        checkpoint = await load_checkpoint(db, current)
        checkpoint.setdefault("pages", {})
        checkpoint.setdefault("regions", {})
        current.phase = "regions"
        current.dispatch_state = "running"
        await db.commit()
        logger.info("content_build_claim build_id=%s fence=%s previous_fence=%s previous_lease_until=%s claimed_at=%s",
                    job.build_id, fence, previous_fence, previous_lease_until, datetime.utcnow().isoformat())
        expected_count = expected_page_count or checkpoint.get("expected_page_count") or (job.profile or {}).get("expected_page_count") or len(pages)
        if expected_count <= 0 or max_regions <= 0:
            raise ValueError("positive page count and region quantum required")
        checkpoint["expected_page_count"] = expected_count
        work = 0
        native_since_save = 0
        # 原生页没有外部计费调用，允许小批量落库，避免每页都序列化整份累计 JSON。
        # OCR/视觉结果仍在返回后立即保存，防止付费结果只存在内存中。
        native_checkpoint_batch = 4
        started = time.monotonic()
        checkpoint.setdefault("prefetched_ocr", {})
        self._ocr_images = {}
        for page_index, page in enumerate(pages):
            number = int(page.get("page_number", page.get("page", 0)))
            if number <= 0:
                continue
            page_cp = checkpoint["pages"].setdefault(str(number), {"page_number": number, "state": "pending", "regions": []})
            regions = self._regions(page, number)
            page_cp["scope_attested"] = bool(
                page.get("regions_scope_complete") is True
                or ((page.get("native_coverage_complete") is True or (page.get("inspection") or {}).get("scope_complete") is True)
                    and all(r.get("kind") == "native" for r in regions))
                or (regions and all(region.get("whole_page") is True for region in regions)
                    and page.get("coverage_attested") is True)
            )
            page_cp["regions"] = [str(r["region_id"]) for r in regions]
            for region_index, region in enumerate(regions):
                rid = str(region["region_id"])
                checkpoint_key = f"p{number}:{rid}"
                native_key = region_key(job.file_hash, number, rid, _digest(str(page.get("text") or "")), job.profile or {})
                if region.get("kind") == "native" and (checkpoint["regions"].get(checkpoint_key) or {}).get("cache_key") == native_key:
                    continue
                if work >= max_regions or (work and max_seconds is not None and time.monotonic() - started >= max_seconds):
                    checkpoint["coverage_manifest"] = self._manifest(checkpoint, expected_count)
                    await self._save(db, job, checkpoint, fence, worker_id, state="yielded")
                    return checkpoint
                # 原生文本标记已覆盖该区域，因此有意不调用 OCR。
                if region.get("kind") == "native" or region.get("source") == "native":
                    result = OCRRegionResult(rid, number, "processed", raw_ocr=str(page.get("text", "") or ""), normalized_ocr=_normalise(page.get("text", "") or ""))
                    checkpoint["regions"][checkpoint_key] = result.as_dict() | {"cache_key": native_key, "source": "native"}
                    work += 1
                    native_since_save += 1
                    if native_since_save >= native_checkpoint_batch:
                        await self._save(db, job, checkpoint, fence, worker_id)
                        native_since_save = 0
                    continue
                prepared = self._ocr_images.pop(checkpoint_key, None)
                image = self._page_image(page, region)
                if prepared:
                    image = prepared["image_bytes"]
                    region = {**region, **prepared["updates"]}
                    if region.get("whole_page") and region.get("scope_complete") is True:
                        page_cp["scope_attested"] = True
                if image is None and renderer is not None:
                    try:
                        rendered = await self._call(renderer, page, region)
                        if isinstance(rendered, Mapping):
                            image = rendered.get("image_bytes")
                            region = {**region, **{k: deepcopy(v) for k, v in rendered.items() if k != "image_bytes"}}
                            if region.get("whole_page") and rendered.get("scope_complete") is True:
                                page_cp["scope_attested"] = True
                        else:
                            image = rendered
                    except Exception as exc:
                        checkpoint["regions"][checkpoint_key] = OCRRegionResult(rid, number, "failed", error=f"render_failed:{type(exc).__name__}").as_dict()
                        await self._save(db, job, checkpoint, fence, worker_id)
                        continue
                if image is None:
                    checkpoint["regions"][checkpoint_key] = OCRRegionResult(rid, number, "unreadable", error="image_unavailable").as_dict()
                    await self._save(db, job, checkpoint, fence, worker_id)
                    continue
                digest = image_hash(image)
                cache_key = region_key(job.file_hash, number, rid, digest, job.profile or {})
                cached = checkpoint["regions"].get(checkpoint_key)
                if cached and cached.get("cache_key") == cache_key and cached.get("state") in {"processed", "blank", "unreadable", "failed"}:
                    continue
                if ocr is None:
                    checkpoint["regions"][checkpoint_key] = OCRRegionResult(rid, number, "unknown", image_hash=digest, error="ocr_not_configured").as_dict()
                    continue
                permit: CircuitPermit | None = None
                if self.circuit:
                    permit = await self.circuit.acquire(service_key_value)
                    if not permit.allowed:
                        job.dispatch_state = "waiting_service"
                        checkpoint["retry_at"] = permit.retry_at.isoformat() if permit.retry_at else None
                        checkpoint["last_wait_reason"] = "ocr_circuit_open"
                        checkpoint["coverage_manifest"] = self._manifest(checkpoint, expected_count)
                        await self._save(db, job, checkpoint, fence, worker_id, state="failed_terminal" if permit.exhausted else "waiting_service", error="ocr_circuit_exhausted" if permit.exhausted else "ocr_circuit_open")
                        raise ServiceUnavailable("OCR service circuit is open", retry_at=permit.retry_at)
                previous = checkpoint["regions"].get(checkpoint_key) or {}
                if previous.get("cache_key") != cache_key:
                    previous = {}
                attempts = int(previous.get("attempts", 0)) + 1
                try:
                    payload = checkpoint["prefetched_ocr"].pop(checkpoint_key, None)
                    if payload is None:
                        partner = await self._prepare_partner_ocr(
                            pages, page_index, region_index, checkpoint, renderer)
                        if partner is None:
                            payload = await self._call(ocr, image, number, region)
                        else:
                            partner_key, partner_image, partner_number, partner_region = partner
                            payload, partner_payload = await asyncio.gather(
                                self._call(ocr, image, number, region),
                                self._call(ocr, partner_image, partner_number, partner_region),
                                return_exceptions=True)
                            if isinstance(payload, Exception):
                                raise payload
                            if not isinstance(partner_payload, Exception) and not (
                                isinstance(partner_payload, Mapping)
                                and partner_payload.get("ocr_outcome") == "capacity"
                            ):
                                checkpoint["prefetched_ocr"][partner_key] = partner_payload
                    if isinstance(payload, Mapping) and payload.get("ocr_outcome") == "capacity":
                        if self.circuit and permit:
                            await self.circuit.release(service_key_value, permit)
                        self._defer(checkpoint, "ocr_capacity", expected_count)
                        await self._save(db, job, checkpoint, fence, worker_id, state="waiting_service",
                                         error="OCR 并发已满，排队后会自动继续，无需重新上传")
                        raise ServiceUnavailable("OCR capacity is full")
                    from_vision = False
                    if isinstance(payload, Mapping) and payload.get("ocr_outcome") == "empty":
                        # OCR 已经成功返回，只是没有文字。不记成服务失败。
                        if self.circuit and permit:
                            await self.circuit.success(service_key_value, permit)
                            permit = None
                        if vision is None:
                            checkpoint["regions"][checkpoint_key] = OCRRegionResult(
                                rid, number, "unreadable", image_hash=digest, error="ocr_empty_no_vision",
                            ).as_dict() | {"cache_key": cache_key, "attempts": attempts}
                            await self._save(db, job, checkpoint, fence, worker_id)
                            continue
                        try:
                            payload = await self._call(vision, image, number, region)
                            from_vision = True
                        except Exception as exc:
                            self._defer(checkpoint, "vision_fallback_unavailable", expected_count)
                            await self._save(db, job, checkpoint, fence, worker_id, state="waiting_service",
                                             error=f"视觉兜底暂不可用，将自动重试：{type(exc).__name__}")
                            raise ServiceUnavailable("vision fallback unavailable") from exc
                    values = self._parse_ocr(payload, rid, number, digest, region)
                    if from_vision:
                        values["source"] = "vision_fallback"
                    self._locate_blocks(values, region, page)
                    values["cache_key"] = cache_key
                    values["attempts"] = attempts
                    checkpoint["regions"][checkpoint_key] = values
                    if self.circuit and permit:
                        await self.circuit.success(service_key_value, permit)
                    work += 1
                except ServiceUnavailable:
                    raise
                except Exception as exc:
                    if self.circuit and permit:
                        await self.circuit.failure(service_key_value, permit)
                    state = "failed" if attempts >= self.max_region_attempts else "unknown"
                    checkpoint["regions"][checkpoint_key] = OCRRegionResult(rid, number, state, image_hash=digest, error=str(exc)).as_dict() | {"cache_key": cache_key, "attempts": attempts}
                    if state == "unknown":
                        job.dispatch_state = "waiting_service"
                        checkpoint["retry_at"] = (datetime.utcnow() + timedelta(seconds=5)).isoformat()
                        checkpoint["coverage_manifest"] = self._manifest(checkpoint, expected_count)
                        await self._save(db, job, checkpoint, fence, worker_id, state="waiting_service", error=type(exc).__name__)
                        raise ServiceUnavailable("OCR request failed; retry is scheduled") from exc
                await self._save(db, job, checkpoint, fence, worker_id)
                native_since_save = 0
            states = [checkpoint["regions"].get(f"p{number}:{rid}", {}).get("state", "unknown") for rid in page_cp["regions"]]
            page_cp["state"] = "processed" if all(s in {"processed", "blank"} for s in states) else "failed" if any(s == "failed" for s in states) else "unknown"
        checkpoint["coverage_manifest"] = self._manifest(checkpoint, expected_count)
        coverage_pages = checkpoint["coverage_manifest"]["pages"]
        checkpoint["progress"] = {"processed_pages": sum(p["scope_complete"] for p in coverage_pages), "total_pages": expected_count}
        ready = all(p["scope_complete"] for p in coverage_pages)
        await self._save(db, job, checkpoint, fence, worker_id, state="succeeded" if ready else "partial", phase="ready" if ready else "partial")
        return checkpoint

    # 为把此阶段称作 build 或 run 的工作进程提供稳定语义别名。
    build = process
    run = process

    @staticmethod
    def _parse_ocr(payload: Any, region_id: str, page_number: int, digest: str, region: Mapping[str, Any]) -> dict[str, Any]:
        if payload is None or payload == [] or payload == {}:
            # 现有 OCR 客户端在服务错误和无文本时都会返回 []。
            # 空白页面需要明确的视觉空白证明，不能仅凭空响应判断。
            raise ContentBuildError("empty OCR response without blank verification")
        if isinstance(payload, Mapping):
            raw = payload.get("raw_ocr", payload.get("text", ""))
            if not str(raw or "").strip() and payload.get("blocks"):
                result = ContentBuildService._parse_ocr(payload["blocks"], region_id, page_number, digest, region)
                result["raw_payload"] = deepcopy(dict(payload))
                return result
            state = payload.get("state") or ("blank" if not raw else "processed")
            if state == "blank" and payload.get("blank_verified") is not True:
                raise ContentBuildError("blank region is not visually verified")
            if state not in {"processed", "blank", "unreadable", "unknown", "failed"}:
                raise ContentBuildError("invalid OCR region state")
            if state == "processed" and not str(raw or "").strip():
                raise ContentBuildError("processed OCR region has no text")
            if state in {"unknown", "failed"}:
                raise ContentBuildError("OCR service did not complete region")
            result = OCRRegionResult(region_id, page_number, state, str(raw or ""), _normalise(payload.get("normalized_ocr", raw)), payload.get("confidence"), payload.get("bbox", region.get("bbox")), payload.get("coordinate_system", region.get("coordinate_system")), digest).as_dict()
            result["raw_payload"] = deepcopy(dict(payload))
            return result
        if isinstance(payload, str):
            if not payload.strip():
                raise ContentBuildError("empty OCR response without blank verification")
            return OCRRegionResult(region_id, page_number, "processed", payload, _normalise(payload), image_hash=digest, bbox=region.get("bbox"), coordinate_system=region.get("coordinate_system")).as_dict()
        items = list(payload)
        blocks = []
        for item in items:
            if isinstance(item, Mapping):
                raw = str(item.get("text", item.get("content", "")) or "")
                block = {"raw_text": raw, "normalized_text": _normalise(raw), "confidence": item.get("confidence"),
                         "locator": deepcopy(item.get("locator")), "bbox": deepcopy(item.get("bbox")), "label": item.get("label")}
            else:
                raw = str(getattr(item, "text", "") or "")
                locator = getattr(item, "locator", None)
                block = {"raw_text": raw, "normalized_text": _normalise(raw), "confidence": getattr(item, "confidence", None),
                         "locator": locator.model_dump(mode="json") if locator is not None and hasattr(locator, "model_dump") else deepcopy(locator),
                         "bbox": None, "label": getattr(item, "label", None)}
            if raw.strip():
                blocks.append(block)
        if not blocks:
            raise ContentBuildError("empty OCR response without blank verification")
        text = "\n".join(str(block["raw_text"]) for block in blocks)
        result = OCRRegionResult(region_id, page_number, "processed", text, _normalise(text), image_hash=digest, bbox=region.get("bbox"), coordinate_system=region.get("coordinate_system"), blocks=blocks).as_dict()
        result["raw_payload"] = [item.model_dump(mode="json") if hasattr(item, "model_dump") else deepcopy(item) for item in items]
        return result


content_build_service = ContentBuildService()
