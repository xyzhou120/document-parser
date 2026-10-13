"""Human review APIs with optimistic concurrency and append-only audit rows."""

import asyncio
import hashlib
from copy import deepcopy
from uuid import UUID

import pymupdf
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import Document, ExtractedFieldModel, ParseRun
from app.models.reviews import FieldReviewRevision
from app.schemas.reviews import FieldReviewRequest, FieldReviewResponse, ReviewedFact
from app.storage import get_db, minio_client

router = APIRouter(prefix="/api/v1/reviews", tags=["人工复核"])


def _field_snapshot(field: ExtractedFieldModel | None) -> dict:
    if field is None:
        return {"field": None}
    return {
        "field_id": str(field.field_id),
        "field_key": field.field_key,
        "field_name": field.field_name,
        "field_value": field.field_value,
        "unit": field.unit,
        "instances": deepcopy(field.instances or []),
        "extraction_detail": deepcopy(field.extraction_detail or {}),
        "evidence": deepcopy(field.evidence or {}),
        "validation_status": field.validation_status,
        # Automatic confidence is deliberately retained as provenance only.
        "confidence": field.confidence,
    }


def _response(row: FieldReviewRevision) -> FieldReviewResponse:
    response = FieldReviewResponse.model_validate(row)
    response.review_status = "REJECTED" if row.action == "REJECT" else "HUMAN_REVIEWED"
    return response


async def _context(db: AsyncSession, parse_run_id: UUID, field_key: str):
    run = await db.get(ParseRun, parse_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="解析运行不存在")
    doc = await db.get(Document, run.document_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="源文档不存在")
    result = await db.execute(
        select(ExtractedFieldModel).where(
            ExtractedFieldModel.parse_run_id == parse_run_id,
            ExtractedFieldModel.field_key == field_key,
        )
    )
    field = result.scalar_one_or_none()
    if field is None:
        raise HTTPException(status_code=404, detail="字段结果不存在")
    return run, doc, field


def _evidence_registry(field: ExtractedFieldModel) -> dict[str, dict]:
    detail = field.extraction_detail or {}
    registry = detail.get("evidence_registry")
    if isinstance(registry, dict):
        return registry
    evidence = field.evidence or {}
    registry = evidence.get("evidence_registry") if isinstance(evidence, dict) else None
    return registry if isinstance(registry, dict) else {}


def _bbox_list(value):
    if value is None:
        return None
    if isinstance(value, dict):
        return [value.get(key) for key in ("x0", "y0", "x1", "y1")]
    return list(value)


def _validate_facts(facts, field: ExtractedFieldModel, artifact_pages: list[dict]):
    registry = _evidence_registry(field)
    page_sizes = {
        int(page["page_number"]): (float(page.get("width", 0)), float(page.get("height", 0)))
        for page in (artifact_pages or [])
        if page.get("page_number") is not None
    }
    for fact in facts:
        for evidence in fact.evidence:
            ref = registry.get(evidence.evidence_id) if evidence.evidence_id else None
            if evidence.evidence_id and ref is None:
                raise HTTPException(status_code=422, detail=f"证据引用不存在: {evidence.evidence_id}")
            if ref and int(ref.get("page_number", -1)) != evidence.page_number:
                raise HTTPException(status_code=422, detail="证据引用页面与事实不一致")
            if evidence.page_number not in page_sizes:
                raise HTTPException(status_code=422, detail="证据页不在源PDF页面范围内")
            if evidence.bbox is None:
                continue
            x0, y0, x1, y1 = map(float, evidence.bbox)
            width, height = page_sizes.get(evidence.page_number, (0, 0))
            if width <= 0 or height <= 0 or not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
                raise HTTPException(status_code=422, detail="证据坐标超出源PDF页面范围")
            if ref and ref.get("bbox") and _bbox_list(ref["bbox"]) != list(map(float, evidence.bbox)):
                raise HTTPException(status_code=422, detail="证据坐标与原提取Evidence不一致")


async def _source_pages(doc: Document) -> list[dict]:
    """Use the hashed original for geometry; cached OCR is never the authority."""
    source = await asyncio.to_thread(minio_client.download, doc.file_path)
    if hashlib.sha256(source).hexdigest() != doc.file_hash:
        raise HTTPException(status_code=409, detail="对象存储源PDF与登记hash不一致")

    def read_bounds():
        with pymupdf.open(stream=source, filetype="pdf") as pdf:
            return [{"page_number": i + 1, "width": page.rect.width, "height": page.rect.height}
                    for i, page in enumerate(pdf)]

    return await asyncio.to_thread(read_bounds)


def _automatic_facts(field: ExtractedFieldModel) -> list[ReviewedFact]:
    registry = _evidence_registry(field)
    instances = field.instances or (field.extraction_detail or {}).get("instances", [])
    facts = []
    for instance in instances:
        refs: list[dict] = []
        ids = list(instance.get("evidence_ids") or [])
        for dim in (instance.get("dimensions") or {}).values():
            if isinstance(dim, dict):
                ids.extend(dim.get("evidence_ids") or [])
        for evidence_id in dict.fromkeys(ids):
            ref = registry.get(evidence_id)
            if ref:
                roles = (instance.get("evidence_roles") or {}).get(evidence_id) or ["value"]
                refs.extend({"evidence_id": evidence_id, "page_number": ref["page_number"],
                             "bbox": _bbox_list(ref.get("bbox")), "coordinate_system": "PDF_POINT",
                             "role": role, "quote": instance.get("quote")} for role in roles)
        dimensions = {key: value.get("value") if isinstance(value, dict) else value
                      for key, value in (instance.get("dimensions") or {}).items()}
        try:
            facts.append(ReviewedFact.model_validate({
                "raw_value": str(instance.get("raw_value", instance.get("value", ""))),
                "value": str(instance.get("value", "")), "unit": instance.get("unit"),
                "value_kind": instance.get("value_kind") or "unknown",
                "source_role": instance.get("source_role") or "unknown",
                "dimensions": dimensions, "evidence": refs,
            }))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="自动事实缺少可核验证据，请使用校正操作补充") from exc
    if not facts:
        raise HTTPException(status_code=422, detail="没有可接受的自动事实，请使用校正操作补充")
    return facts


@router.get("/runs/{parse_run_id}/fields/{field_key}", response_model=dict, summary="查询字段复核历史")
async def get_field_review_history(parse_run_id: UUID, field_key: str, db: AsyncSession = Depends(get_db)):
    _run, _doc, field = await _context(db, parse_run_id, field_key)
    result = await db.execute(
        select(FieldReviewRevision)
        .where(FieldReviewRevision.parse_run_id == parse_run_id, FieldReviewRevision.field_key == field_key)
        .order_by(FieldReviewRevision.revision.asc())
    )
    revisions = result.scalars().all()
    return {
        "parse_run_id": parse_run_id,
        "field_key": field_key,
        "automatic": _field_snapshot(field),
        "latest_revision": revisions[-1].revision if revisions else 0,
        "review_status": _response(revisions[-1]).review_status if revisions else "UNREVIEWED",
        "history": [_response(row) for row in revisions],
    }


@router.post("/runs/{parse_run_id}/fields/{field_key}", response_model=FieldReviewResponse,
            summary="提交字段人工复核")
async def review_field(
    parse_run_id: UUID,
    field_key: str,
    payload: FieldReviewRequest,
    db: AsyncSession = Depends(get_db),
):
    # Serializes submissions on the same run, including the first revision.
    locked = await db.execute(select(ParseRun).where(ParseRun.parse_run_id == parse_run_id).with_for_update())
    if locked.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="解析运行不存在")
    run, doc, field = await _context(db, parse_run_id, field_key)
    if run.status not in {"COMPLETED", "FAILED"}:
        raise HTTPException(status_code=409, detail="运行尚未结束，不能提交人工复核")
    if payload.file_hash != doc.file_hash:
        raise HTTPException(status_code=409, detail="文件hash已变化，拒绝写入复核结果")
    result = await db.execute(
        select(FieldReviewRevision)
        .where(FieldReviewRevision.parse_run_id == parse_run_id, FieldReviewRevision.field_key == field_key)
        .order_by(FieldReviewRevision.revision.desc())
    )
    latest = result.scalars().first()
    actual_revision = latest.revision if latest else 0
    if payload.expected_revision != actual_revision:
        raise HTTPException(status_code=409, detail=f"复核版本冲突，当前版本为{actual_revision}")
    if payload.action == "ACCEPT":
        if latest:
            if not latest.facts:
                raise HTTPException(status_code=422, detail="当前审核事实为空，请校正补充后再接受")
            facts = [ReviewedFact.model_validate(fact) for fact in latest.facts]
        else:
            facts = _automatic_facts(field)
    else:
        facts = payload.facts
    pages = await _source_pages(doc)
    _validate_facts(facts, field, pages)
    row = FieldReviewRevision(
        parse_run_id=parse_run_id,
        document_id=doc.document_id,
        field_key=field_key,
        revision=actual_revision + 1,
        previous_revision=actual_revision,
        action=payload.action,
        reviewer=payload.reviewer,
        reason=payload.reason,
        file_hash=doc.file_hash,
        before_snapshot={"automatic": _field_snapshot(field),
                         "previous_review": {"review_id": str(latest.review_id),
                                             "revision": latest.revision, "action": latest.action,
                                             "reviewer": latest.reviewer, "facts": deepcopy(latest.facts)}
                         if latest else None},
        facts=[fact.model_dump(mode="json") for fact in facts],
    )
    db.add(row)
    await db.flush()
    return _response(row)


@router.get("/runs/{parse_run_id}/export", response_model=dict, summary="导出人工复核事实")
async def export_reviewed_facts(parse_run_id: UUID, db: AsyncSession = Depends(get_db)):
    run = await db.get(ParseRun, parse_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="解析运行不存在")
    result = await db.execute(
        select(FieldReviewRevision)
        .where(FieldReviewRevision.parse_run_id == parse_run_id)
        .order_by(FieldReviewRevision.field_key, FieldReviewRevision.revision.desc())
    )
    latest: dict[str, FieldReviewRevision] = {}
    for row in result.scalars().all():
        latest.setdefault(row.field_key, row)
    return {
        "parse_run_id": parse_run_id,
        "document_id": run.document_id,
        "source": "human_review_revisions",
        "facts": [
            {"field_key": row.field_key, "action": row.action, "reviewer": row.reviewer,
             "revision": row.revision, "facts": row.facts, "created_at": row.created_at,
             "file_hash": row.file_hash, "review_status": _response(row).review_status,
             "review_id": row.review_id, "reason": row.reason}
            for row in latest.values()
        ],
    }
