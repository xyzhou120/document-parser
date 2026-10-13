"""Conservative source correction: immutable parents and explicit full re-extraction."""

import hashlib
import math
from copy import deepcopy
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.database import Document, DocumentArtifact, ParseRun
from app.models.source_revisions import SourceRevision
from app.schemas.source_revisions import SourceRevisionPayload


def text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def correct_page(page: dict, payload: SourceRevisionPayload, revision_id: UUID) -> dict:
    """Apply an exact span, shifting intact blocks and invalidating overlapping structure."""
    updated = deepcopy(page)
    original = str(page.get("text") or "")
    start, end = payload.start_offset, payload.end_offset
    if start is None or end is None or end > len(original) or original[start:end] != payload.original_text:
        raise HTTPException(409, "原文 text offset 与当前 Artifact 不一致")
    if payload.original_text_hash and payload.original_text_hash != text_hash(original[start:end]):
        raise HTTPException(409, "原文 hash 不一致")
    replacement = payload.replacement_text or ""
    updated["text"] = original[:start] + replacement + original[end:]
    native_original = str(page.get("native_text") or "")
    overlapping = [block for block in page.get("text_blocks") or []
                   if isinstance(block.get("start_offset"), int) and isinstance(block.get("end_offset"), int)
                   and block["start_offset"] < end and block["end_offset"] > start]
    ocr_based = any(block.get("source") in {"ocr", "automatic_ocr"} or block.get("ocr_based") for block in overlapping)
    # Keep the raw native layer separate on mixed/scanned pages.  A human correction
    # of an OCR span must never turn the entire OCR page into a native text layer.
    if not ocr_based and end <= len(native_original) and native_original[start:end] == payload.original_text:
        updated["native_text"] = native_original[:start] + replacement + native_original[end:]
    else:
        updated["native_text"] = native_original
    updated["superseded_source_blocks"] = [*deepcopy(page.get("superseded_source_blocks") or []), *deepcopy(overlapping)]
    delta = len(replacement) - (end - start)
    blocks = []
    for block in page.get("text_blocks") or []:
        block = deepcopy(block)
        left, right = block.get("start_offset"), block.get("end_offset")
        if not isinstance(left, int) or not isinstance(right, int):
            continue
        if right <= start:
            blocks.append(block)
        elif left >= end:
            block["start_offset"] += delta
            block["end_offset"] += delta
            blocks.append(block)
    blocks.append({"text": replacement, "start_offset": start, "end_offset": start + len(replacement),
                   "bbox": [payload.bbox[key] for key in ("x0", "y0", "x1", "y1")] if payload.bbox else None,
                   "coordinate_system": "PDF_POINT", "source": "human_correction",
                   "ocr_based": ocr_based, "raw_ocr": payload.original_text if ocr_based else None,
                   "corrected_transcription": replacement,
                   "source_revision_id": str(revision_id)})
    updated["text_blocks"] = sorted(blocks, key=lambda block: block["start_offset"])
    # Exact table-cell/ocr dependencies are not currently tracked.  Keeping these
    # would let a strategy read the superseded value after a text correction.
    updated["tables"] = []
    updated["ocr_blocks"] = []
    updated["source_review_status"] = "PARTIALLY_REVIEWED"
    updated["source_revision_id"] = str(revision_id)
    updated["invalidated_structures"] = ["tables", "ocr_blocks", "overlapping_text_blocks"]
    return updated


def validate_bbox(bbox: dict | None, page: dict):
    if bbox is None:
        return
    if set(bbox) != {"x0", "y0", "x1", "y1"}:
        raise HTTPException(422, "bbox 必须包含 x0/y0/x1/y1（PDF_POINT）")
    x0, y0, x1, y1 = (bbox[key] for key in ("x0", "y0", "x1", "y1"))
    if not all(math.isfinite(value) for value in (x0, y0, x1, y1)):
        raise HTTPException(422, "bbox 必须为有限坐标")
    if not (0 <= x0 < x1 <= page.get("width", 0) and 0 <= y0 < y1 <= page.get("height", 0)):
        raise HTTPException(422, "bbox 超出页面范围")


class SourceRevisionService:
    async def document(self, db: AsyncSession, document_id: UUID, *, lock=False):
        query = select(Document).where(Document.document_id == document_id)
        if lock:
            query = query.with_for_update().execution_options(populate_existing=True)
        document = (await db.execute(query)).scalar_one_or_none()
        if document is None:
            raise HTTPException(404, "文档不存在")
        return document

    async def artifact(self, db: AsyncSession, artifact_id: UUID):
        artifact = await db.get(DocumentArtifact, artifact_id)
        if artifact is None:
            raise HTTPException(404, "Artifact 不存在")
        return artifact

    async def latest(self, db: AsyncSession, document_id: UUID):
        return await db.scalar(select(SourceRevision).where(SourceRevision.document_id == document_id)
                               .order_by(SourceRevision.created_at.desc()).limit(1))

    async def revision(self, db: AsyncSession, revision_id: UUID):
        row = await db.get(SourceRevision, revision_id)
        if row is None:
            raise HTTPException(404, "源文修订不存在")
        return row

    async def _rules_profile(self, db, document, payload):
        from app.rules.repository import rule_repository
        from app.services.parse_service import rule_snapshot
        if payload.rule_bundle_id:
            from app.services.release_service import bundle_specs, release_service, validate_bundle
            bundle = await release_service.get(db, payload.rule_bundle_id)
            await release_service._checklist(db, document.checklist_type)
            if bundle.checklist_type != document.checklist_type or not validate_bundle(bundle)["valid"]:
                raise HTTPException(422, "试跑 bundle 与清单不匹配或校验失败")
            return {**rule_snapshot(bundle_specs(bundle), document.checklist_type, "checklist_agent"),
                    "rule_bundle_id": str(bundle.bundle_id)}
        if payload.parent_run_id:
            run = await db.get(ParseRun, payload.parent_run_id)
            if run is None or run.document_id != document.document_id:
                raise HTTPException(422, "父运行不属于该文档")
            if not (run.extraction_profile or {}).get("rules"):
                raise HTTPException(422, "父运行缺少固定规则快照")
            from app.services.artifact_service import preprocessing_profile
            from app.services.parse_service import snapshot_rules
            engine = run.extraction_profile.get("extraction_engine", "rules")
            # Keep rules/model settings immutable, but reset run-result metadata.
            profile = rule_snapshot(snapshot_rules(run.extraction_profile), document.checklist_type, engine)
            for key in ("agent_config", "agent_model", "rule_bundle_id"):
                if key in run.extraction_profile:
                    profile[key] = deepcopy(run.extraction_profile[key])
            profile["preprocess_profile_version"] = preprocessing_profile(engine)["version"]
            return profile
        rules = await rule_repository.get_rules_for_document(db, document)
        if not rules:
            raise HTTPException(422, "清单没有启用规则")
        return rule_snapshot(rules, document.checklist_type, "checklist_agent")

    async def create(self, db: AsyncSession, payload: SourceRevisionPayload):
        if settings.app.reliable_enabled:
            ready = await db.scalar(sql_text("SELECT to_regclass('content_revisions') IS NOT NULL AND to_regclass('document_processing_heads') IS NOT NULL AND to_regclass('parse_field_jobs') IS NOT NULL AND to_regclass('execution_outbox') IS NOT NULL"))
            if ready is not True:
                raise HTTPException(503, "可靠内容发布结构未就绪，请先完成迁移")
        document = await self.document(db, payload.document_id, lock=True)
        if document.file_hash != payload.expected_file_hash:
            raise HTTPException(409, "文件指纹已变化")
        artifact = await self.artifact(db, payload.parent_artifact_id)
        if artifact.document_id != document.document_id or artifact.file_hash != document.file_hash:
            raise HTTPException(422, "Artifact 不属于当前文档版本")
        latest = await self.latest(db, document.document_id)
        expected_artifact = (latest.derived_artifact_id or latest.parent_artifact_id) if latest else artifact.artifact_id
        if artifact.artifact_id != expected_artifact or payload.expected_parent_revision_id != (
                latest.revision_id if latest else None):
            raise HTTPException(409, "源文修订基版本已变化，请刷新后重试")
        pages = deepcopy(artifact.pages)
        page = next((page for page in pages if page.get("page_number") == payload.page_number), None)
        if page is None:
            raise HTTPException(422, "页面不存在")
        validate_bbox(payload.bbox, page)
        revision_id = uuid4()
        derived = None
        if payload.revision_type in {"CORRECT", "UNREADABLE"}:
            if payload.revision_type == "CORRECT":
                pages[pages.index(page)] = correct_page(page, payload, revision_id)
            else:
                page["source_review_status"] = "PARTIALLY_REVIEWED"
                page["source_unreadable"] = True
                page["source_revision_id"] = str(revision_id)
            quality = {**deepcopy(artifact.quality or {}), "source_review_status": "PARTIALLY_REVIEWED",
                       "parent_artifact_id": str(artifact.artifact_id),
                       "source_revision_id": str(revision_id),
                       "source_revision_chain": [*(artifact.quality or {}).get("source_revision_chain", []), str(revision_id)],
                       "source_corrections": [*(artifact.quality or {}).get("source_corrections", []),
                                              {"page_number": payload.page_number,
                                               "revision_type": payload.revision_type,
                                               "original_text": payload.original_text,
                                               "replacement_text": payload.replacement_text,
                                               "start_offset": payload.start_offset,
                                               "end_offset": payload.end_offset,
                                               "bbox": deepcopy(payload.bbox)}],
                       "dependency_precision": "UNKNOWN", "recompute_policy": "FULL"}
            if payload.revision_type == "UNREADABLE":
                quality["source_unreadable_pages"] = sorted(set(quality.get("source_unreadable_pages", [])) | {payload.page_number})
                quality["weak_pages"] = sorted(set(quality.get("weak_pages", [])) | {payload.page_number})
            derived = DocumentArtifact(artifact_id=uuid4(), document_id=document.document_id,
                                       file_hash=artifact.file_hash,
                                       preprocess_profile_version=artifact.preprocess_profile_version,
                                       status=artifact.status, pages=pages, quality=quality)
            db.add(derived)
        elif payload.original_text is not None:
            start, end = payload.start_offset, payload.end_offset
            if start is None or end is None or (page.get("text") or "")[start:end] != payload.original_text:
                raise HTTPException(409, "确认区域原文与 Artifact 不一致")
        if settings.app.reliable_enabled:
            # Content changes preserve the existing frozen rule/model denominator.
            # No current rule repository lookup is allowed on this branch.
            from app.models.content import DocumentProcessingHead
            head = await db.get(DocumentProcessingHead, document.document_id)
            selected_run = await db.get(ParseRun, payload.parent_run_id) if payload.parent_run_id else await db.scalar(
                select(ParseRun).where(ParseRun.document_id == document.document_id,
                    ParseRun.content_revision_id == (head.active_content_revision_id if head else None),
                    ParseRun.contract_version == "reliable-extraction-v2").order_by(ParseRun.created_at.desc()).limit(1))
            if payload.parent_run_id and (selected_run is None or selected_run.document_id != document.document_id
                                           or selected_run.contract_version != "reliable-extraction-v2"):
                raise HTTPException(409, "可靠模式父运行必须属于本文档的固定内容版本")
            profile = deepcopy(selected_run.extraction_profile or {}) if selected_run else {"rules": []}
        else:
            selected_run = None
            profile = await self._rules_profile(db, document, payload)
        # User supplied field lists are hints only; absent dependency tracking
        # requires conservative full re-extraction of every active field.
        fields = [rule["field_key"] for rule in profile["rules"]]
        revision = SourceRevision(revision_id=revision_id, document_id=document.document_id,
                                  parent_artifact_id=artifact.artifact_id,
                                  derived_artifact_id=derived.artifact_id if derived else None,
                                  parent_revision_id=latest.revision_id if latest else None,
                                  revision_type=payload.revision_type, page_number=payload.page_number,
                                  bbox=deepcopy(payload.bbox), start_offset=payload.start_offset,
                                  end_offset=payload.end_offset, original_text=payload.original_text,
                                  original_text_hash=text_hash(payload.original_text) if payload.original_text is not None else None,
                                  replacement_text=payload.replacement_text, comment=payload.comment,
                                  affected_fields=fields, created_by=payload.created_by,
                                  source_review_status="PARTIALLY_REVIEWED")
        run = None
        if payload.start_extraction and not settings.app.reliable_enabled:
            run = self.make_run(document, derived or artifact, revision, profile, payload.parent_run_id)
            revision.extraction_run_id = run.parse_run_id
            revision.extraction_task_id = str(run.parse_run_id)
            db.add(run)
        db.add(revision)
        await db.flush()
        if settings.app.reliable_enabled:
            # Reliable mode bridges the append-only human audit into P11's content
            # publisher.  Legacy mode above remains byte-for-byte compatible.
            from app.services.content_publish_service import content_publish_service
            head = await content_publish_service._head(db, document.document_id, lock=True)
            if head.active_content_revision_id:
                from app.models.content import ContentRevision
                active = await db.get(ContentRevision, head.active_content_revision_id)
                if active is None or active.artifact_id != artifact.artifact_id:
                    raise HTTPException(409, "活动内容版本已变化，请刷新后订正")
            source_run = selected_run
            if source_run is not None and source_run.contract_version != "reliable-extraction-v2":
                raise HTTPException(409, "可靠模式的父运行必须使用 reliable-extraction-v2")
            if derived is not None:
                result = await content_publish_service.publish_artifact(
                    db, derived, expected_parent_revision_id=head.active_content_revision_id,
                    expected_cas_version=head.cas_version,
                    origin_source_revision_id=revision.revision_id,
                    request_id=f"source-revision:{revision.revision_id}",
                    source_run_id=source_run.parse_run_id if source_run else None,
                    changed_ranges=[{"page_number": revision.page_number, "bbox": deepcopy(revision.bbox)}],
                    source_kind="human_correction", change_kind=revision.revision_type.lower(),
                    expected_invalidation_watermark=head.invalidation_watermark,
                )
                successor = result.get("successor")
                # SourceRevision has already been appended and is immutable.  The
                # successor is resolved through ContentRevision.origin_source_revision_id.
                await db.commit()
                return revision, successor
            # CONFIRM has no derived text; retain an audit row without inventing a
            # content revision.  A later explicit enrichment can publish it.
            await db.commit()
            return revision, None
        await db.commit()
        if run:
            await self.submit(db, run)
        return revision, run

    def make_run(self, document, artifact, revision, profile, parent_run_id=None):
        if artifact.status != "READY":
            raise HTTPException(409, "只允许使用 READY Artifact 重算；PARTIAL 不能被订正升级为 READY")
        if profile.get("preprocess_profile_version") != artifact.preprocess_profile_version:
            raise HTTPException(409, "前处理版本不同，请先完成当前版本前处理")
        return ParseRun(parse_run_id=uuid4(), document_id=document.document_id,
                        checklist_type=document.checklist_type, parent_parse_run_id=parent_run_id,
                        artifact_id=artifact.artifact_id, run_mode="FULL", cache_status="ARTIFACT_HIT",
                        status="PENDING", requested_field_keys=None,
                        extraction_profile={**profile, "reuse_artifact_id": str(artifact.artifact_id),
                                            "source_revision_id": str(revision.revision_id),
                                            "source_corrections": deepcopy((artifact.quality or {}).get("source_corrections")) or [{"page_number": revision.page_number,
                                                                    "revision_type": revision.revision_type,
                                                                    "original_text": revision.original_text,
                                                                    "replacement_text": revision.replacement_text}],
                                            "source_review_status": "PARTIALLY_REVIEWED",
                                            "source_recompute_policy": "FULL", "force_refresh": False})

    async def submit(self, db: AsyncSession, run):
        from app.tasks import parse_document_task
        try:
            parse_document_task.apply_async(args=[str(run.document_id), str(run.parse_run_id)],
                                            task_id=str(run.parse_run_id))
        except Exception:
            run.status = "FAILED"
            run.error_message = "源文订正重算提交失败；可显式重试，修订与旧结果已保留"
            run.extraction_profile = {**run.extraction_profile, "source_dispatch_failed": True}
            await db.commit()

    async def retry(self, db: AsyncSession, revision_id: UUID):
        revision = await self.revision(db, revision_id)
        await self.document(db, revision.document_id, lock=True)
        run = await db.get(ParseRun, revision.extraction_run_id) if revision.extraction_run_id else await db.scalar(
            select(ParseRun).where(ParseRun.document_id == revision.document_id,
                                   ParseRun.extraction_profile["source_revision_id"].as_string() == str(revision_id))
            .order_by(ParseRun.created_at.desc()).limit(1))
        if run is None:
            raise HTTPException(409, "重算运行缺失")
        if run.status != "FAILED" or not (run.extraction_profile or {}).get("source_dispatch_failed"):
            return run
        latest = await self.latest(db, revision.document_id)
        if latest and latest.revision_id != revision_id:
            raise HTTPException(409, "已有更新修订，请重算最新版本")
        run.status = "PENDING"
        run.error_message = None
        run.extraction_profile = {**(run.extraction_profile or {}), "source_dispatch_failed": False}
        await db.commit()
        await self.submit(db, run)
        return run

    async def preview(self, db: AsyncSession, payload: SourceRevisionPayload):
        document = await self.document(db, payload.document_id)
        artifact = await self.artifact(db, payload.parent_artifact_id)
        if (document.file_hash != payload.expected_file_hash or artifact.file_hash != document.file_hash
                or artifact.document_id != document.document_id):
            raise HTTPException(409, "文件或 Artifact 版本不匹配")
        latest = await self.latest(db, document.document_id)
        expected_artifact = latest.derived_artifact_id or latest.parent_artifact_id if latest else artifact.artifact_id
        if artifact.artifact_id != expected_artifact or payload.expected_parent_revision_id != (
                latest.revision_id if latest else None):
            raise HTTPException(409, "源文修订基版本已变化")
        page = next((page for page in artifact.pages if page.get("page_number") == payload.page_number), None)
        if page is None:
            raise HTTPException(422, "页面不存在")
        validate_bbox(payload.bbox, page)
        corrected = correct_page(page, payload, uuid4()) if payload.revision_type == "CORRECT" else page
        profile = await self._rules_profile(db, document, payload)
        return {"mode": "FULL", "dependency_precision": "UNKNOWN", "model_called": False,
                "affected_fields": [rule["field_key"] for rule in profile["rules"]],
                "source_review_status": "PARTIALLY_REVIEWED", "raw_facts_status": "UNVERIFIED",
                "original_text": page.get("text"), "derived_text": corrected.get("text"),
                "invalidated_structures": corrected.get("invalidated_structures", []),
                "extraction_available": artifact.status == "READY"}

    async def extract(self, db: AsyncSession, revision_id: UUID, payload):
        revision = await self.revision(db, revision_id)
        document = await self.document(db, revision.document_id, lock=True)
        # This idempotency key is scoped to both document and source revision.
        existing = await db.scalar(select(ParseRun).where(
            ParseRun.document_id == document.document_id,
            ParseRun.extraction_profile["source_revision_id"].as_string() == str(revision_id),
            ParseRun.extraction_profile["source_request_id"].as_string() == str(payload.request_id)).limit(1))
        if existing is not None:
            return existing
        latest = await self.latest(db, document.document_id)
        if latest and latest.revision_id != revision_id:
            raise HTTPException(409, "已有更新源文修订，请使用最新版本重算")
        artifact = await self.artifact(db, revision.derived_artifact_id or revision.parent_artifact_id)
        if artifact.file_hash != document.file_hash:
            raise HTTPException(409, "源文文件版本已变化")
        profile = await self._rules_profile(db, document, payload)
        profile["source_request_id"] = str(payload.request_id)
        run = self.make_run(document, artifact, revision, profile, payload.parent_run_id)
        db.add(run)
        await db.flush()
        await db.commit()
        await self.submit(db, run)
        return run

    async def impact(self, db: AsyncSession, revision_id: UUID):
        revision = await self.revision(db, revision_id)
        runs = (await db.execute(select(ParseRun).where(ParseRun.document_id == revision.document_id,
                                                        ParseRun.artifact_id == revision.parent_artifact_id))).scalars().all()
        return {"revision_id": revision_id, "source_review_status": "PARTIALLY_REVIEWED",
                "affected_fields": revision.affected_fields,
                "extraction_plan": {"mode": "FULL", "dependency_precision": "UNKNOWN",
                                    "reason": "尚无完整区域到字段依赖图，不能承诺局部安全",
                                    "reuse_artifact_id": revision.derived_artifact_id,
                                    "raw_facts_status": "UNVERIFIED"},
                "stale_run_ids": [run.parse_run_id for run in runs] if revision.derived_artifact_id else []}


async def source_state(db: AsyncSession, run: ParseRun) -> dict:
    """Fact consumers can flag changed upstream content without accepting any fact."""
    if not run.artifact_id:
        return {"source_review_status": "UNVERIFIED", "stale_source": False, "revision_ids": [],
                "source_state_evaluated": False}
    artifact = await db.get(DocumentArtifact, run.artifact_id)
    revisions = (await db.execute(select(SourceRevision).where(
        SourceRevision.document_id == run.document_id).order_by(SourceRevision.created_at))).scalars().all()
    superseding = [revision for revision in revisions if revision.parent_artifact_id == run.artifact_id
                   and revision.derived_artifact_id is not None]
    derived_revisions = [revision for revision in revisions if revision.derived_artifact_id is not None]
    detached_from_head = False
    if derived_revisions and artifact:
        head_revision = derived_revisions[-1]
        head_artifact = await db.get(DocumentArtifact, head_revision.derived_artifact_id)
        detached_from_head = bool(head_artifact and head_artifact.file_hash == artifact.file_hash
                                  and head_artifact.artifact_id != run.artifact_id)
    relevant = [revision for revision in revisions if revision.parent_artifact_id == run.artifact_id
                or revision.derived_artifact_id == run.artifact_id]
    quality = (artifact.quality or {}) if artifact else {}
    unreadable = set(quality.get("source_unreadable_pages", [])) | {
        row.page_number for row in relevant if row.revision_type == "UNREADABLE"}
    return {"source_review_status": "PARTIALLY_REVIEWED" if relevant else quality.get("source_review_status", "UNVERIFIED"),
            "stale_source": bool(superseding) or detached_from_head,
            "source_revision_not_applied": detached_from_head,
            "revision_ids": [str(row.revision_id) for row in (revisions if detached_from_head else relevant)],
            "source_unreadable_pages": sorted(unreadable),
            "source_state_evaluated": artifact is not None}


source_revision_service = SourceRevisionService()
