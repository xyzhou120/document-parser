"""持久化批量导入与按完整筛选条件导出事实，不受分页大小截断。"""

import copy
import hashlib
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.batches import Batch, BatchItem
from app.models.database import Document, ExtractedFieldModel, ParseRun
from app.services.batch_service import BatchUploadSession, batch_summary, facts_csv, item_status
from app.storage import get_db
from app.tasks import parse_document_task

router = APIRouter(prefix="/api/v1/batches", tags=["批次"])


class ItemInput(BaseModel):
    client_key: str = Field(min_length=1, max_length=200)
    file_name: str = Field(min_length=1, max_length=500)
    file_hash: str | None = Field(default=None, pattern=r"^[a-fA-F0-9]{64}$")
    mine_id: str = Field(min_length=1, max_length=100)
    mine_name: str = Field(min_length=1, max_length=200)
    checklist_type: str = Field(min_length=1, max_length=100)
    reporting_period: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    extraction_engine: str = "checklist_agent"

    @field_validator("extraction_engine")
    @classmethod
    def supported_engine(cls, value):
        if value not in {"checklist_agent", "rules"}:
            raise ValueError("不支持的提取引擎")
        return value

    @field_validator("client_key", "file_name", "mine_id", "mine_name", "checklist_type")
    @classmethod
    def not_blank(cls, value):
        if not value.strip():
            raise ValueError("不能为空")
        return value.strip()


class BatchInput(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    items: list[ItemInput] = Field(min_length=1, max_length=500)


class VersionInput(BaseModel):
    version: int = Field(ge=1)


def serialize_item(item, run=None):
    return {
        "item_id": item.item_id,
        "client_key": item.client_key,
        "file_name": item.file_name,
        "file_hash": item.file_hash,
        "file_size": item.file_size,
        "metadata": item.metadata_json,
        "document_id": item.document_id,
        "parse_run_id": item.parse_run_id,
        "status": item_status(item, run),
        "version": item.version,
        "attempts": item.attempts,
        "error_message": (
            run.error_message if run and run.status == "FAILED" else item.error_message
        ),
    }


async def locked_item(db, batch_id, item_id):
    item = (
        await db.execute(
            select(BatchItem)
            .where(BatchItem.batch_id == batch_id, BatchItem.item_id == item_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if item is None:
        raise HTTPException(404, "批次文件不存在")
    return item


def check_version(item, version):
    if item.version != version:
        raise HTTPException(409, "文件状态已更新，请刷新后重试")


@router.post("")
async def create_batch(payload: BatchInput, db: AsyncSession = Depends(get_db)):
    if len({item.client_key for item in payload.items}) != len(payload.items):
        raise HTTPException(422, "批次内 client_key 不能重复")
    batch = Batch(name=payload.name.strip(), status="PENDING", version=1)
    if not batch.name:
        raise HTTPException(422, "批次名称不能为空")
    db.add(batch)
    await db.flush()
    for entry in payload.items:
        metadata = entry.model_dump(exclude={"client_key", "file_name", "file_hash"})
        db.add(
            BatchItem(
                batch_id=batch.batch_id,
                client_key=entry.client_key,
                file_name=entry.file_name,
                file_hash=entry.file_hash.lower() if entry.file_hash else None,
                metadata_json=metadata,
                status="PENDING",
                version=1,
                attempts=0,
            )
        )
    await db.commit()
    return await get_batch(batch.batch_id, db)


async def detail(db, batch):
    items = (
        (
            await db.execute(
                select(BatchItem)
                .where(BatchItem.batch_id == batch.batch_id)
                .order_by(BatchItem.created_at, BatchItem.item_id)
            )
        )
        .scalars()
        .all()
    )
    ids = [item.parse_run_id for item in items if item.parse_run_id]
    runs = (
        (await db.execute(select(ParseRun).where(ParseRun.parse_run_id.in_(ids)))).scalars().all()
        if ids
        else []
    )
    mapping = {run.parse_run_id: run for run in runs}
    summary = batch_summary(items, mapping)
    return {
        "batch_id": batch.batch_id,
        "name": batch.name,
        "version": batch.version,
        "created_at": batch.created_at,
        **summary,
        "items": [serialize_item(item, mapping.get(item.parse_run_id)) for item in items],
    }


@router.get("")
async def list_batches(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    total = (await db.execute(select(func.count()).select_from(Batch))).scalar_one()
    batches = (
        (
            await db.execute(
                select(Batch)
                .order_by(Batch.created_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )
    summaries = []
    for batch in batches:
        row = await detail(db, batch)
        row.pop("items")
        summaries.append(row)
    return {"total": total, "items": summaries}


# 静态路由必须放在 UUID 详情路由之前。
@router.get("/export")
async def export_facts(
    batch_id: UUID | None = None,
    mine_id: str | None = None,
    checklist_type: str | None = None,
    reporting_period: str | None = None,
    status: str | None = None,
    format: str = Query("json", pattern="^(json|csv)$"),
    db: AsyncSession = Depends(get_db),
):
    from app.services.fact_service import build_fact_view

    query = select(ParseRun, Document).join(Document, ParseRun.document_id == Document.document_id)
    if batch_id:
        query = query.join(BatchItem, BatchItem.parse_run_id == ParseRun.parse_run_id).where(
            BatchItem.batch_id == batch_id
        )
    for column, value in [
        (Document.mine_id, mine_id),
        (Document.checklist_type, checklist_type),
        (Document.report_period, reporting_period),
        (ParseRun.status, status),
    ]:
        if value:
            query = query.where(column == value)
    query = query.order_by(ParseRun.created_at, ParseRun.parse_run_id)
    rows = []
    # 分批读取全部匹配的字段与事实；不套用界面分页限制。
    offset = 0
    run_count = 0
    while True:
        pairs = (await db.execute(query.offset(offset).limit(100))).all()
        if not pairs:
            break
        ids = [run.parse_run_id for run, _ in pairs]
        fields = (
            (
                await db.execute(
                    select(ExtractedFieldModel).where(ExtractedFieldModel.parse_run_id.in_(ids))
                )
            )
            .scalars()
            .all()
        )
        fields_by_run = {}
        for field in fields:
            fields_by_run.setdefault(field.parse_run_id, []).append(field)
        for run, document in pairs:
            run_count += 1
            view = build_fact_view(run, fields_by_run.get(run.parse_run_id, []))
            base = {
                "parse_run_id": str(run.parse_run_id),
                "document_id": str(document.document_id),
                "file_name": document.file_name,
                "mine_id": document.mine_id,
                "mine_name": document.mine_name,
                "checklist_type": document.checklist_type,
                "reporting_period": document.report_period,
                "status": run.status,
            }
            facts = [fact.model_dump(mode="json") for fact in view.facts]
            for fact in facts:
                rows.append({**base, "fact": fact})
            for missing in view.missing_fields:
                rows.append(
                    {
                        **base,
                        "field_key": missing.get("field_key"),
                        "field_name": missing.get("field_name"),
                        "fact": {},
                        "export_note": "MISSING_FIELD",
                        "missing_field": missing,
                    }
                )
            if not facts and not view.missing_fields:
                rows.append({**base, "fact": {}, "export_note": "NO_FACTS"})
        offset += len(pairs)
    if format == "csv":
        return Response(
            facts_csv(rows),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="field-facts.csv"'},
        )
    return {"view": "automatic", "run_count": run_count, "row_count": len(rows), "items": rows}


@router.get("/{batch_id}")
async def get_batch(batch_id: UUID, db: AsyncSession = Depends(get_db)):
    batch = await db.get(Batch, batch_id)
    if not batch:
        raise HTTPException(404, "批次不存在")
    return await detail(db, batch)


@router.post("/{batch_id}/items/{item_id}/upload")
async def upload_item(
    batch_id: UUID,
    item_id: UUID,
    version: int = Form(..., ge=1),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
):
    from app.api.parse import upload_and_parse

    item = await locked_item(db, batch_id, item_id)
    limit = min(settings.upload.max_file_size, 100 * 1024 * 1024)
    content = await file.read(limit + 1)
    if len(content) > limit:
        raise HTTPException(413, "文件超过上传大小限制")
    file_hash = hashlib.sha256(content).hexdigest()
    if item.file_hash and item.file_hash != file_hash:
        raise HTTPException(409, "文件内容与批次登记哈希不一致，不能替换已有文件")
    if file.filename != item.file_name:
        raise HTTPException(409, "请重新选择批次登记的原文件")
    if item.document_id:
        return serialize_item(item, await db.get(ParseRun, item.parse_run_id))
    check_version(item, version)
    if item.status not in {"PENDING", "UPLOAD_FAILED"}:
        raise HTTPException(409, "当前状态不允许上传")
    item.file_hash = file_hash
    item.status = "UPLOADING"
    item.attempts += 1
    item.version += 1
    item.error_message = None
    await file.seek(0)
    try:
        await upload_and_parse(file=file, db=BatchUploadSession(db, item), **item.metadata_json)
    except Exception as exc:
        # 队列失败时，已持久化文档的关联写入已经原子完成，应保留以便重试解析；
        # 其他存储错误则回滚并标记上传失败。
        await db.rollback()
        item = await locked_item(db, batch_id, item_id)
        if not item.document_id:
            item.status = "UPLOAD_FAILED"
            item.file_hash = file_hash
            item.attempts += 1
            item.version += 1
            item.error_message = (
                str(exc.detail) if isinstance(exc, HTTPException) else "上传失败，请重试"
            )
        await db.commit()
        raise
    return serialize_item(item, await db.get(ParseRun, item.parse_run_id))


@router.post("/{batch_id}/items/{item_id}/retry")
async def retry_item(
    batch_id: UUID, item_id: UUID, payload: VersionInput, db: AsyncSession = Depends(get_db)
):
    item = await locked_item(db, batch_id, item_id)
    check_version(item, payload.version)
    if not item.document_id or not item.parse_run_id:
        raise HTTPException(409, "上传失败请重新选择原文件后上传")
    previous = await db.get(ParseRun, item.parse_run_id)
    if not previous or previous.status != "FAILED":
        raise HTTPException(409, "仅解析失败的文件允许重试")
    parent_id = (
        previous.parent_parse_run_id
        if previous.run_mode == "INCREMENTAL"
        else previous.parse_run_id
    )
    run = ParseRun(
        document_id=item.document_id,
        parent_parse_run_id=parent_id,
        artifact_id=previous.artifact_id,
        run_mode=previous.run_mode or "FULL",
        requested_field_keys=copy.deepcopy(previous.requested_field_keys),
        checklist_type=previous.checklist_type,
        extraction_profile=copy.deepcopy(previous.extraction_profile),
        status="PENDING",
    )
    db.add(run)
    await db.flush()
    item.parse_run_id, item.status, item.error_message = run.parse_run_id, "PENDING", None
    item.attempts += 1
    item.version += 1
    await db.commit()
    try:
        parse_document_task.delay(str(item.document_id), str(run.parse_run_id))
    except Exception as exc:
        run.status = "FAILED"
        run.error_message = "队列提交失败，可稍后重试"
        item.status, item.error_message = "PARSE_FAILED", run.error_message
        await db.commit()
        raise HTTPException(503, run.error_message) from exc
    return serialize_item(item, run)


@router.post("/{batch_id}/items/{item_id}/cancel")
async def cancel_item(
    batch_id: UUID, item_id: UUID, payload: VersionInput, db: AsyncSession = Depends(get_db)
):
    item = await locked_item(db, batch_id, item_id)
    check_version(item, payload.version)
    if item.document_id or item.status not in {"PENDING", "UPLOAD_FAILED"}:
        raise HTTPException(409, "仅尚未提交解析的文件允许取消；已提交任务会继续执行")
    item.status, item.error_message = "CANCELLED", None
    item.version += 1
    await db.commit()
    return serialize_item(item)
