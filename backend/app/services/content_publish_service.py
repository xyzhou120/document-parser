"""Artifact 内容修订的事务性发布。

本模块只负责当前内容的发布。它不会修改不可变的修订、影响或失效记录，
也不会发布索引版本。
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.content import (
    ContentInvalidation,
    ContentRevision,
    DocumentProcessingHead,
    IndexGeneration,
)
from app.models.database import Document, DocumentArtifact, ParseRun
from app.models.execution import ExecutionOutbox, ParseFieldJob
from app.services.content_impact_service import ContentImpactService, content_impact_service
from app.services.content_service import ContentRevisionError, ContentService, content_service


class ContentPublicationError(RuntimeError):
    pass


class ContentPublishService:
    def __init__(self, content: ContentService | None = None, impacts: ContentImpactService | None = None):
        self.content = content or content_service
        self.impacts = impacts or content_impact_service

    async def _head(self, db: AsyncSession, document_id: UUID, *, lock: bool = True):
        await db.execute(insert(DocumentProcessingHead).values(document_id=document_id, cas_version=0, invalidation_watermark=0)
                         .on_conflict_do_nothing(index_elements=["document_id"]))
        query = select(DocumentProcessingHead).where(DocumentProcessingHead.document_id == document_id)
        if lock:
            query = query.with_for_update().execution_options(populate_existing=True)
        head = await db.scalar(query)
        return head

    async def _supersede_runs(self, db: AsyncSession, document_id: UUID, revision_id: UUID) -> list[UUID]:
        from app.services.execution.field_scheduler import ACTIVE_FIELD_STATES
        runs = list((await db.execute(select(ParseRun).where(
            ParseRun.document_id == document_id,
            ParseRun.content_revision_id != revision_id,
            ParseRun.contract_version == "reliable-extraction-v2",
            ParseRun.status.in_(["PLANNED", "PENDING", "RUNNING", "YIELDED", "WAITING_SERVICE", "WAITING_EXTERNAL_QUOTA", "PARTIAL", "PAUSED"])
        ).order_by(ParseRun.parse_run_id).with_for_update().execution_options(populate_existing=True))).scalars().all())
        superseded: list[UUID] = []
        for run in runs:
            run.status = "SUPERSEDED"
            run.execution_generation = int(run.execution_generation or 0) + 1
            jobs = (await db.execute(select(ParseFieldJob).where(ParseFieldJob.run_id == run.parse_run_id).order_by(ParseFieldJob.job_id)
                                     .with_for_update().execution_options(populate_existing=True))).scalars().all()
            for job in jobs:
                if job.execution_state in ACTIVE_FIELD_STATES | {"paused"}:
                    job.execution_state = "superseded"
                    job.semantic_state = "incomplete"
                job.fence_token += 1
                job.lease_owner = None
                job.lease_until = None
            superseded.append(run.parse_run_id)
        return superseded

    async def _successor(self, db: AsyncSession, *, source_run: ParseRun | None, document_id: UUID,
                         artifact: DocumentArtifact, revision: ContentRevision, request_id: str | None,
                         invalidation_watermark: int = 0, content_cas_version: int = 0) -> ParseRun | None:
        if source_run is None:
            return None
        if (source_run.extraction_profile or {}).get("attachment_version_id"):
            # 内容订正使旧快照失效；新业务提取必须显式经过版本门禁，
            # 不能复制旧 version_id 创建另一个竞争运行。
            return None
        if source_run.contract_version != "reliable-extraction-v2" or source_run.document_id != document_id:
            raise ContentPublicationError("successor requires a frozen reliable run from this document")
        profile = deepcopy(source_run.extraction_profile or {})
        for result_key in ("agent_runtime", "source_dispatch_failed", "automatic_complete", "coverage", "failures"):
            profile.pop(result_key, None)
        profile["reuse_artifact_id"] = str(artifact.artifact_id)
        profile["content_revision_id"] = str(revision.revision_id)
        profile["source_request_id"] = request_id
        if revision.origin_source_revision_id:
            profile["source_revision_id"] = str(revision.origin_source_revision_id)
        profile["source_recompute_policy"] = "FULL"
        profile["invalidation_watermark"] = int(invalidation_watermark)
        profile["content_invalidation_watermark"] = int(invalidation_watermark)  # 旧版工作进程别名
        profile["content_cas_version"] = int(content_cas_version)
        # 后继记录按来源修订和请求 ID 保持幂等，并始终使用旧的冻结配置；
        # 不读取当前规则。
        for candidate in (await db.execute(select(ParseRun).where(
            ParseRun.document_id == document_id,
            ParseRun.content_revision_id == revision.revision_id))).scalars().all():
            if (candidate.extraction_profile or {}).get("source_request_id") == request_id:
                return candidate
        successor = ParseRun(parse_run_id=uuid4(), document_id=document_id,
                             parent_parse_run_id=source_run.parse_run_id, artifact_id=artifact.artifact_id,
                             content_revision_id=revision.revision_id, contract_version=source_run.contract_version,
                             execution_generation=0, requested_field_keys=None, run_mode="FULL",
                             checklist_type=source_run.checklist_type, extraction_profile=profile,
                             status="PLANNED", cache_status="ARTIFACT_HIT")
        db.add(successor)
        await db.flush()
        # P05 负责数据模式和字段记录初始化。此处保留持久化事件，
        # 不在持有头锁时导入工作进程或启动运行。
        from app.services.execution.job_repository import JobRepository
        old_jobs = list((await db.execute(select(ParseFieldJob).where(ParseFieldJob.run_id == source_run.parse_run_id))).scalars().all())
        if not old_jobs or {j.field_key for j in old_jobs} != {r["field_key"] for r in profile.get("rules") or []}:
            raise ContentPublicationError("frozen source field plan is missing or inconsistent")
        fields = [{"field_key": j.field_key, "rule_hash": j.rule_hash, "snapshot": deepcopy((j.checkpoint or {}).get("rule_snapshot", {}))} for j in old_jobs]
        repo = JobRepository(db)
        jobs = await repo.initialize_jobs(successor.parse_run_id, revision_id=revision.revision_id, fields=fields)
        for job in jobs:
            await repo.enqueue_outbox(kind="field_job_wakeup", aggregate_id=job.job_id,
                                      dedupe_key=f"{job.job_id}:content:{revision.revision_id}",
                                      payload={"run_id": str(successor.parse_run_id), "job_id": str(job.job_id), "revision_id": str(revision.revision_id)})
        return successor

    async def publish_artifact(
        self, db: AsyncSession, artifact: DocumentArtifact, *, expected_parent_revision_id: UUID | None = None,
        expected_cas_version: int | None = None, origin_source_revision_id: UUID | None = None,
        origin_event_id: str | None = None, source_kind: str = "artifact", change_kind: str = "initial",
        changed_ranges: list[dict[str, Any]] | None = None, request_id: str | None = None,
        source_run_id: UUID | None = None, invalidation_kind: str = "content",
        expected_invalidation_watermark: int | None = None,
        suppress_successor: bool = False,
    ) -> dict[str, Any]:
        document_id = UUID(str(artifact.document_id))
        origin_event_id = origin_event_id or (f"request:{request_id}" if request_id else None)
        document = await db.get(Document, document_id)
        if document is None or document.file_hash != artifact.file_hash:
            raise ContentPublicationError("artifact is not the current document source")
        head = await self._head(db, document_id, lock=True)
        current_id = head.active_content_revision_id
        if request_id:
            receipt = await db.scalar(select(ExecutionOutbox).where(
                ExecutionOutbox.kind == "content_publication_receipt",
                ExecutionOutbox.dedupe_key == f"{document_id}:{request_id}"))
            if receipt:
                if receipt.payload["artifact_id"] != str(artifact.artifact_id):
                    raise ContentPublicationError("publication request already maps another artifact")
                recorded = await self.content.read_revision(db, UUID(receipt.payload["revision_id"]))
                return {"revision": recorded.revision, "head": head, "successor": None, "idempotent": True, "unchanged": True}
        existing = None
        if origin_source_revision_id or origin_event_id:
            identity = ContentRevision.origin_source_revision_id == origin_source_revision_id if origin_source_revision_id else ContentRevision.origin_event_id == origin_event_id
            existing = await db.scalar(select(ContentRevision).where(ContentRevision.document_id == document_id, identity))
        if existing is not None:
            if existing.artifact_id != artifact.artifact_id:
                raise ContentPublicationError("publication request already maps another artifact")
            await self.content.read_revision(db, existing.revision_id)
            successor = await db.scalar(select(ParseRun).where(ParseRun.content_revision_id == existing.revision_id).order_by(ParseRun.created_at).limit(1))
            return {"revision": existing, "head": head, "successor": successor, "idempotent": True}
        if expected_cas_version is None or head.cas_version != expected_cas_version:
            raise ContentPublicationError("processing head CAS conflict")
        if current_id != expected_parent_revision_id:
            raise ContentPublicationError("active content revision changed")
        if expected_invalidation_watermark is not None and head.invalidation_watermark != expected_invalidation_watermark:
            raise ContentPublicationError("invalidation watermark changed")
        if current_id is not None and expected_invalidation_watermark is None:
            raise ContentPublicationError("publication requires an invalidation watermark snapshot")
        if current_id:
            current = await self.content.read_revision(db, current_id)
            incoming = self.content.adapt_artifact(artifact)
            if (current.content.content_digest == incoming.content_digest
                    and current.content.coverage_digest == incoming.coverage_digest
                    and current.revision.preprocess_manifest == (artifact.quality or {}).get("preprocess_manifest", {})):
                if request_id:
                    db.add(ExecutionOutbox(kind="content_publication_receipt", aggregate_id=current.revision.revision_id,
                                           dedupe_key=f"{document_id}:{request_id}",
                                           payload={"artifact_id": str(artifact.artifact_id), "revision_id": str(current.revision.revision_id)},
                                           dispatch_state="sent", attempts=0))
                    await db.flush()
                return {"revision": current.revision, "head": head, "successor": None, "idempotent": True, "unchanged": True}
        try:
            pinned = await self.content.pin_revision(db, artifact, parent_revision_id=current_id,
                                                     origin_source_revision_id=origin_source_revision_id,
                                                     origin_event_id=origin_event_id, source_kind=source_kind,
                                                     change_kind=change_kind)
        except ContentRevisionError as exc:
            raise ContentPublicationError(str(exc)) from exc
        revision = pinned.revision
        if head.active_content_revision_id == revision.revision_id:
            return {"revision": revision, "head": head, "successor": None, "idempotent": True}
        source_run = await db.get(ParseRun, source_run_id) if source_run_id else (None if suppress_successor else await db.scalar(select(ParseRun).where(
            ParseRun.document_id == document_id, ParseRun.content_revision_id == current_id,
            ParseRun.contract_version == "reliable-extraction-v2", ParseRun.status.notin_(["CANCELLED", "SUPERSEDED"])).order_by(ParseRun.created_at.desc()).limit(1)))
        if source_run is not None and source_run.content_revision_id != current_id:
            raise ContentPublicationError("source run does not match expected parent content")
        superseded = await self._supersede_runs(db, document_id, revision.revision_id)
        head.active_content_revision_id = revision.revision_id
        head.cas_version = int(head.cas_version or 0) + 1
        changed_ranges = changed_ranges or []
        selector = {"scope": "revision", "revision_id": str(current_id) if current_id else None,
                    "content_revision_id": str(current_id) if current_id else None,
                    "request_id": f"{document_id}:{request_id or origin_event_id or origin_source_revision_id or revision.revision_id}"}
        impact = None
        if current_id:
            impact = await self.impacts.append_impact(db, from_revision_id=current_id, to_revision_id=revision.revision_id,
                                                      changed_ranges=changed_ranges, dependency_precision="unknown",
                                                      recompute_policy="full", invalidation_scope=selector, request_id=selector["request_id"])
            invalidation = await self.impacts.append_invalidation(db, document_id=document_id, kind=invalidation_kind,
                                                                  selector=selector, reason="content revision published")
            generations = list((await db.execute(select(IndexGeneration).where(IndexGeneration.revision_id == current_id)
                                                  .order_by(IndexGeneration.generation_id).with_for_update().execution_options(populate_existing=True))).scalars().all())
            for generation in generations:
                generation.serve_status = "blocked"
                generation.serve_version += 1
        else:
            invalidation = None
        successor = await self._successor(db, source_run=source_run, document_id=document_id, artifact=artifact,
                                          revision=revision, request_id=request_id,
                                          invalidation_watermark=int(head.invalidation_watermark or 0),
                                          content_cas_version=int(head.cas_version or 0))
        from app.services.execution.job_repository import JobRepository
        await JobRepository(db).enqueue_outbox(kind="content_index_build", aggregate_id=revision.revision_id,
                                              dedupe_key=f"index-content:{revision.revision_id}",
                                              payload={"revision_id": str(revision.revision_id), "document_id": str(document_id),
                                                       "invalidation_watermark": head.invalidation_watermark, "cas_version": head.cas_version})
        await db.flush()
        return {"revision": revision, "head": head, "impact": impact, "invalidation": invalidation,
                "successor": successor, "superseded_run_ids": superseded, "idempotent": False}

    publish = publish_artifact
    publish_revision = publish_artifact

    async def publish_verified_ocr(
        self, db: AsyncSession, *, run_id: UUID, candidate: dict[str, Any],
        receipt: dict[str, Any], corrected_transcription: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """将一项图像核验后的 OCR 修正发布为新的不可变修订。

        调用方必须提供视觉核验器返回的回执。此方法拒绝只有模型支持的建议、猜测
        以及来自其他内容修订的块，然后克隆 Artifact，并将 CAS、失效记录与后继处理
        委托给 publish_artifact。
        """
        run = await db.get(ParseRun, UUID(str(run_id)))
        if run is None or run.contract_version != "reliable-extraction-v2" or not run.content_revision_id:
            raise ContentPublicationError("OCR correction requires a reliable frozen run")
        revision = await db.get(ContentRevision, run.content_revision_id)
        artifact = await db.get(DocumentArtifact, run.artifact_id)
        if revision is None or artifact is None or revision.artifact_id != artifact.artifact_id:
            raise ContentPublicationError("frozen run source is unavailable")
        verification = (receipt or {}).get("verification") or {}
        image_ref = (receipt or {}).get("image_ref") or {}
        candidate_id = str(candidate.get("candidate_id") or "")
        raw_ocr = str(candidate.get("raw_ocr") or "")
        corrected = str(corrected_transcription or candidate.get("corrected_transcription") or "")
        candidate_page = int(candidate.get("page_number") or 0)
        candidate_region = str(candidate.get("region_id") or candidate.get("block_id") or "")
        image_hash = str(candidate.get("image_hash") or "")
        if not candidate_id or not raw_ocr or not corrected or corrected == raw_ocr:
            raise ContentPublicationError("OCR correction candidate is empty or unchanged")
        if ((receipt or {}).get("source") != "original_image" or str((receipt or {}).get("revision_id")) != str(revision.revision_id)
                or (receipt or {}).get("candidate_id") != candidate_id or (receipt or {}).get("region_id") != candidate_region
                or verification.get("status") != "verified" or verification.get("transcription") != corrected
                or not image_hash or image_ref.get("image_hash") != image_hash):
            raise ContentPublicationError("OCR verification receipt does not match frozen candidate")
        attrs = verification.get("attributes") or {}
        if not (attrs.get("value") or {}).get("evidence_ids") or not (attrs.get("value") or {}).get("basis"):
            raise ContentPublicationError("OCR receipt lacks supported value evidence")
        event_id = f"ocr-correction:{run.document_id}:{candidate_id}"
        head = await self._head(db, artifact.document_id, lock=True)
        previous = await db.scalar(select(ContentRevision).where(ContentRevision.document_id == run.document_id,
                                                                  ContentRevision.origin_event_id == event_id))
        if previous:
            prior_artifact = await db.get(DocumentArtifact, previous.artifact_id)
            if prior_artifact is None:
                raise ContentPublicationError("prior OCR publication source is unavailable")
            saved = (prior_artifact.quality or {}).get("ocr_corrections") or []
            match = next((item for item in saved if item["candidate_id"] == candidate_id), None)
            if not match or match["raw_ocr"] != raw_ocr or match["corrected_transcription"] != corrected or match["image_hash"] != image_hash:
                raise ContentPublicationError("OCR correction request conflicts with prior publication")
            successor = await db.scalar(select(ParseRun).where(ParseRun.content_revision_id == previous.revision_id).order_by(ParseRun.created_at).limit(1))
            return {"revision": previous, "successor": successor, "head": head, "idempotent": True}
        if head.active_content_revision_id != revision.revision_id:
            raise ContentPublicationError("OCR candidate revision is no longer active")
        pages = deepcopy(artifact.pages or [])
        target = None
        target_page = None
        for page in pages:
            if int(page.get("page_number", 0)) != candidate_page:
                continue
            target_page = page
            adapted = self.content.adapt_artifact(artifact)
            adapted_page = next(p for p in adapted.pages if p["page_number"] == candidate_page)
            matches = [index for index, block in enumerate(adapted_page.get("text_blocks") or [])
                       if candidate_region in {str(block.get("block_id") or ""), str(block.get("region_id") or "")}]
            if len(matches) == 1 and matches[0] < len(page.get("text_blocks") or []):
                target = page["text_blocks"][matches[0]]
            if target is not None:
                break
        if target_page is None or target is None:
            raise ContentPublicationError("OCR candidate block is not present in frozen Artifact")
        if target.get("source") not in {"ocr", "automatic_ocr"}:
            raise ContentPublicationError("OCR correction cannot rewrite a native text block")
        span_start = candidate.get("span_start")
        span_end = candidate.get("span_end")
        full_block_text = str(target.get("text") or target.get("raw_ocr") or "")
        if span_start is not None or span_end is not None:
            if not isinstance(span_start, int) or not isinstance(span_end, int) or not (0 <= span_start < span_end <= len(full_block_text)):
                raise ContentPublicationError("OCR correction span is invalid")
            if full_block_text[span_start:span_end] != raw_ocr:
                raise ContentPublicationError("OCR correction span text changed")
        else:
            span_start, span_end = 0, len(full_block_text)
        if candidate.get("bbox") is not None and candidate["bbox"] != target.get("bbox"):
            raise ContentPublicationError("OCR correction location does not match source block")
        if candidate.get("coordinate_system") is not None and candidate["coordinate_system"] != target.get("coordinate_system"):
            raise ContentPublicationError("OCR correction coordinate space does not match source block")
        observed_hash = str(target.get("image_hash") or (target.get("image_ref") or {}).get("image_hash") or "")
        if observed_hash and observed_hash != image_hash:
            raise ContentPublicationError("OCR image hash does not match candidate block")
        if span_start == 0 and span_end == len(full_block_text) and full_block_text != raw_ocr:
            raise ContentPublicationError("OCR raw transcription changed since candidate creation")
        target["raw_ocr"] = target.get("raw_ocr") or full_block_text
        target["corrected_transcription"] = corrected
        target["text"] = corrected
        target["normalized_text"] = corrected
        target["source"] = "automatic_ocr"
        target["image_hash"] = image_hash
        target["verification_receipt"] = deepcopy(receipt)
        # 保留原始 OCR 及图像定位器，同时让新搜索文本与精确范围保持一致。
        # 后续块按差值移动。
        left, right = target.get("start_offset"), target.get("end_offset")
        if not isinstance(left, int) or not isinstance(right, int) or str(target_page.get("text") or "")[left + span_start:left + span_end] != raw_ocr:
            raise ContentPublicationError("OCR correction requires an exact current text span")
        absolute_start, absolute_end = left + span_start, left + span_end
        target_page["text"] = target_page["text"][:absolute_start] + corrected + target_page["text"][absolute_end:]
        delta = len(corrected) - (absolute_end - absolute_start)
        target["text"] = full_block_text[:span_start] + corrected + full_block_text[span_end:]
        target["normalized_text"] = target["text"]
        target["corrected_transcription"] = target["text"]
        target["end_offset"] = right + delta
        target["correction_span"] = {"start": span_start, "end": span_start + len(corrected), "raw_ocr": raw_ocr}
        for other in target_page.get("text_blocks") or []:
            if other is target:
                continue
            if isinstance(other.get("start_offset"), int) and other["start_offset"] >= absolute_end:
                other["start_offset"] += delta
                other["end_offset"] += delta
        for raw_block in target_page.get("ocr_blocks") or []:
            if raw_block.get("text") == raw_ocr and raw_block.get("locator") == target.get("original_locator"):
                raw_block["raw_ocr"] = raw_ocr
                raw_block["text"] = corrected
                raw_block["corrected_transcription"] = corrected
        quality = deepcopy(artifact.quality or {})
        quality["ocr_corrections"] = [*(quality.get("ocr_corrections") or []), {
            "candidate_id": candidate_id, "page_number": candidate_page, "region_id": candidate_region,
            "raw_ocr": raw_ocr, "corrected_transcription": corrected, "image_hash": image_hash,
            "source": "original_image", "requires_publication": False,
        }]
        derived = DocumentArtifact(document_id=artifact.document_id, file_hash=artifact.file_hash,
                                   preprocess_profile_version=artifact.preprocess_profile_version,
                                   status=artifact.status, pages=pages, quality=quality)
        db.add(derived)
        await db.flush()
        return await self.publish_artifact(
            db, derived, expected_parent_revision_id=revision.revision_id,
            expected_cas_version=head.cas_version,
            expected_invalidation_watermark=head.invalidation_watermark,
            origin_event_id=event_id, request_id=request_id or f"ocr:{candidate_id}",
            source_run_id=run.parse_run_id, source_kind="automatic_ocr", change_kind="ocr_correction",
            changed_ranges=[{"page_number": candidate_page, "block_id": candidate_region}],
        )

    async def guard_revision(self, db: AsyncSession, *, document_id: UUID, revision_id: UUID,
                             expected_invalidation_watermark: int | None = None):
        """先取得头锁，再取得运行或任务锁，并持有到结果提交。"""
        head = await self._head(db, document_id)
        if head.active_content_revision_id != revision_id:
            raise ContentPublicationError("content revision is no longer active")
        if expected_invalidation_watermark is not None and head.invalidation_watermark != expected_invalidation_watermark:
            raise ContentPublicationError("invalidation watermark changed")
        invalidations = (await db.execute(select(ContentInvalidation).where(
            ContentInvalidation.document_id == document_id,
            ContentInvalidation.effective_at <= datetime.utcnow()))).scalars().all()
        for row in invalidations:
            selector = row.scope_selector or {}
            scope = selector.get("scope") or selector.get("kind") or row.invalidation_kind
            if scope in {"field", "fact", "parse_run", "generation"}:
                continue
            if selector.get("generation_id") or selector.get("index_generation_id"):
                continue
            selected = selector.get("revision_id") or selector.get("content_revision_id")
            if selected is None or str(selected) == str(revision_id):
                raise ContentPublicationError("content revision source is invalidated")
        return head

    async def publish_parse_run(self, db: AsyncSession, run_id: UUID, *, expected_cas_version: int,
                                expected_invalidation_watermark: int,
                                expected_current_parse_run_id: UUID | None = None):
        run = await db.get(ParseRun, run_id)
        if run is None or run.contract_version != "reliable-extraction-v2" or run.content_revision_id is None:
            raise ContentPublicationError("only reliable extraction results can be published")
        head = await self.guard_revision(db, document_id=run.document_id, revision_id=run.content_revision_id,
                                         expected_invalidation_watermark=expected_invalidation_watermark)
        if head.cas_version != expected_cas_version or head.current_parse_run_id != expected_current_parse_run_id:
            raise ContentPublicationError("parse pointer CAS conflict")
        run = await db.get(ParseRun, run_id, with_for_update=True, populate_existing=True)
        if run is None:
            raise ContentPublicationError("parse run is no longer available")
        if head.current_parse_run_id and head.current_parse_run_id != run_id:
            previous = await db.get(ParseRun, head.current_parse_run_id)
            if previous and previous.content_revision_id == run.content_revision_id and previous.created_at > run.created_at:
                raise ContentPublicationError("an older run cannot replace newer results")
        jobs = list((await db.execute(select(ParseFieldJob).where(ParseFieldJob.run_id == run_id))).scalars().all())
        from app.services.execution.field_scheduler import aggregate_run_status
        if not jobs or aggregate_run_status(run, jobs) != "COMPLETED":
            raise ContentPublicationError("parse run is not automatically complete")
        head.current_parse_run_id = run_id
        head.cas_version += 1
        await db.flush()
        return head


content_publish_service = ContentPublishService()
