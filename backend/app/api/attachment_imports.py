"""Excel 导入暂存与运营接口。模板内容仅作为数据，不执行其中的指令。"""
from datetime import datetime
from hashlib import sha256
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.models.attachments import Attachment, AttachmentChecklistMapping, AttachmentImport, AttachmentImportRow, AttachmentVersion
from app.models.rules import ChecklistType
from app.security import get_principal
from app.storage import get_db

router = APIRouter(prefix='/api/v1/imports', tags=['Excel 附件导入'])


def enabled():
    if not settings.attachment_import.enabled:
        raise HTTPException(404, 'Excel 导入入口已关闭')


async def require_import(db, import_id, lock=False):
    row = await db.get(AttachmentImport, import_id, with_for_update=lock, populate_existing=True)
    if row is None:
        raise HTTPException(404, '导入批次不存在')
    return row


def batch_dict(batch):
    return {k: getattr(batch, k) for k in ('id', 'source_system', 'file_name', 'file_hash', 'state',
        'counts', 'mappings', 'last_error', 'actor', 'created_at', 'confirmed_at')}


def wakeup():
    from app.tasks import attachment_import_recovery_task
    try:
        attachment_import_recovery_task.delay()
    except Exception:
        pass  # Beat 从持久化状态补发；不能将已受理请求报告成失败。


@router.post('/excel', status_code=202)
async def preview(request: Request, file: UploadFile = File(...),
                  source_system: str = Form('upstream', min_length=1, max_length=100), db=Depends(get_db)):
    enabled()
    if not file.filename or not file.filename.lower().endswith('.xlsx'):
        raise HTTPException(422, '请上传 .xlsx 模板文件')
    if not source_system.strip() or source_system.strip() == 'manual':
        raise HTTPException(422, '来源命名空间无效')
    chunks, size = [], 0
    while part := await file.read(64 * 1024):
        size += len(part)
        if size > settings.attachment_import.max_upload_bytes:
            raise HTTPException(413, 'Excel 大小超过限制')
        chunks.append(part)
    data = b''.join(chunks)
    batch = AttachmentImport(source_system=source_system.strip(), file_name=file.filename[-500:],
        file_hash=sha256(data).hexdigest(), staging_file=data, actor=get_principal(request).actor,
        state='PREVIEW_QUEUED')
    db.add(batch)
    await db.commit()
    wakeup()
    return batch_dict(batch)


@router.get('')
async def list_imports(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100), db=Depends(get_db)):
    total = await db.scalar(select(func.count()).select_from(AttachmentImport))
    rows = (await db.scalars(select(AttachmentImport).order_by(AttachmentImport.created_at.desc())
        .limit(page_size).offset((page - 1) * page_size))).all()
    return {'total': total, 'items': [batch_dict(r) for r in rows]}


@router.get('/{import_id}')
async def import_detail(import_id: UUID, page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
                        state: str | None = None, file_name_id: str | None = None, db=Depends(get_db)):
    batch = await require_import(db, import_id)
    base = select(AttachmentImportRow).where(AttachmentImportRow.import_id == import_id)
    if state:
        base = base.where(AttachmentImportRow.state == state)
    if file_name_id:
        base = base.where(AttachmentImportRow.normalized['file_name_id'].as_string() == file_name_id)
    total = await db.scalar(select(func.count()).select_from(base.subquery()))
    rows = (await db.scalars(base.order_by(AttachmentImportRow.sheet, AttachmentImportRow.row_number)
        .limit(page_size).offset((page - 1) * page_size))).all()
    registration = dict((await db.execute(select(AttachmentImportRow.state, func.count())
        .where(AttachmentImportRow.import_id == import_id).group_by(AttachmentImportRow.state))).all())
    ids = select(AttachmentImportRow.attachment_id).where(AttachmentImportRow.import_id == import_id,
        AttachmentImportRow.attachment_id.is_not(None)).distinct()
    download = dict((await db.execute(select(Attachment.download_state, func.count()).where(Attachment.id.in_(ids))
        .group_by(Attachment.download_state))).all())
    result = dict((await db.execute(select(Attachment.status, func.count()).where(Attachment.id.in_(ids))
        .group_by(Attachment.status))).all())
    latest_version = select(AttachmentVersion.attachment_id, func.max(AttachmentVersion.number).label('number'))\
        .where(AttachmentVersion.attachment_id.in_(ids)).group_by(AttachmentVersion.attachment_id).subquery()
    parsing = dict((await db.execute(select(AttachmentVersion.state, func.count()).join(latest_version,
        (AttachmentVersion.attachment_id == latest_version.c.attachment_id) & (AttachmentVersion.number == latest_version.c.number))
        .group_by(AttachmentVersion.state))).all())
    return {**batch_dict(batch), 'summary': {'registration': registration, 'download': download, 'parsing': parsing, 'result': result},
        'total': total, 'items': [{**{k: getattr(r, k) for k in ('id', 'sheet', 'row_number', 'state', 'errors', 'attachment_id')},
                                 'data': r.normalized} for r in rows]}


@router.get('/{import_id}/groups')
async def checklist_groups(import_id: UUID, page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200), db=Depends(get_db)):
    batch = await require_import(db, import_id)
    key = AttachmentImportRow.normalized['file_name_id'].as_string()
    name = AttachmentImportRow.normalized['file_name'].as_string()
    query = select(key.label('file_name_id'), func.min(name).label('file_name'), func.count().label('count'))\
        .where(AttachmentImportRow.import_id == import_id, AttachmentImportRow.state != 'INVALID').group_by(key)
    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    rows = (await db.execute(query.order_by(key).limit(page_size).offset((page - 1) * page_size))).mappings().all()
    return {'total': total, 'items': [{**r, 'checklist_type': batch.mappings.get(r['file_name_id'])} for r in rows]}


class MappingInput(BaseModel):
    mappings: dict[str, str] = Field(max_length=1000)


@router.post('/{import_id}/mappings')
async def set_mappings(import_id: UUID, payload: MappingInput, request: Request, db=Depends(get_db)):
    enabled()
    batch = await require_import(db, import_id, True)
    if batch.state not in {'PREVIEW_READY', 'ACCEPTED'}:
        raise HTTPException(409, '此阶段不能修改清单映射')
    allowed = set((await db.scalars(select(ChecklistType.checklist_type).where(ChecklistType.is_active.is_(True),
        ChecklistType.checklist_type.in_(set(payload.mappings.values()))))).all())
    if any(not key.strip() or len(key) > 200 or val not in allowed for key, val in payload.mappings.items()):
        raise HTTPException(422, '清单映射无效或内部清单未启用')
    batch.mappings = {**batch.mappings, **payload.mappings}
    for key, value in payload.mappings.items():
        await db.execute(insert(AttachmentChecklistMapping).values(source_system=batch.source_system, file_name_id=key,
            checklist_type=value, actor=get_principal(request).actor).on_conflict_do_update(
            constraint='uq_attachment_checklist_mapping', set_={'checklist_type': value, 'actor': get_principal(request).actor,
                'revision': AttachmentChecklistMapping.revision + 1, 'updated_at': datetime.utcnow()}))
        if batch.state == 'ACCEPTED':
            # 只填尚无映射的范围；已有映射不静默变更，已提取结果不被重算。
            from app.models.database import Document
            scope = (Attachment.source_system == batch.source_system,
                     Attachment.file_name_id == key, Attachment.checklist_type.is_(None))
            # 先锁关联文档，后更新附件，避免与提取路径反向加锁。
            await db.execute(select(Document).join(Attachment, Attachment.document_id == Document.document_id)
                .where(*scope).order_by(Document.document_id).with_for_update(of=Document))
            ids = (await db.scalars(select(Attachment.document_id).where(*scope))).all()
            await db.execute(update(Attachment).where(*scope).values(checklist_type=value,
                status='READY'))
            await db.execute(update(Document).where(Document.document_id.in_([x for x in ids if x]))
                .values(checklist_type=value))
    await db.commit()
    wakeup()
    return batch_dict(batch)


class ConfirmInput(BaseModel):
    row_ids: list[UUID] | None = Field(default=None, max_length=1000)
    file_name_ids: list[str] | None = Field(default=None, max_length=1000)


@router.post('/{import_id}/confirm', status_code=202)
async def confirm(import_id: UUID, payload: ConfirmInput, db=Depends(get_db)):
    enabled()
    batch = await require_import(db, import_id, True)
    if batch.state == 'REGISTERING':
        return batch_dict(batch)
    if batch.state not in {'PREVIEW_READY', 'ACCEPTED'}:
        raise HTTPException(409, '预览尚未完成或已失效')
    # 冻结已保存的映射；未映射 PDF 仍能登记/下载，不能启动错误规则。
    query = update(AttachmentImportRow).where(AttachmentImportRow.import_id == import_id, AttachmentImportRow.state == 'VALID')
    if payload.row_ids is not None:
        query = query.where(AttachmentImportRow.id.in_(payload.row_ids))
    if payload.file_name_ids is not None:
        query = query.where(AttachmentImportRow.normalized['file_name_id'].as_string().in_(payload.file_name_ids))
    result = await db.execute(query.values(state='SELECTED'))
    if result.rowcount == 0 and batch.state == 'ACCEPTED':
        return batch_dict(batch)
    if result.rowcount == 0:
        raise HTTPException(422, '没有可确认的有效行')
    batch.state, batch.confirmed_at = 'REGISTERING', datetime.utcnow()
    await db.commit()
    wakeup()
    return batch_dict(batch)


@router.get('/{import_id}/errors')
async def errors_export(import_id: UUID, db=Depends(get_db)):
    await require_import(db, import_id)
    # 错误清单按工作表/行号定位，分块输出。
    export_sessions = async_sessionmaker(db.bind, expire_on_commit=False)
    async def rows(export_db):
        import csv
        import io
        yield '\ufeff工作表,行号,状态,错误\r\n'
        result = await export_db.stream(select(AttachmentImportRow.sheet, AttachmentImportRow.row_number,
            AttachmentImportRow.state, AttachmentImportRow.errors).where(AttachmentImportRow.import_id == import_id,
            AttachmentImportRow.state.in_(['INVALID', 'CONFLICT'])).order_by(AttachmentImportRow.sheet, AttachmentImportRow.row_number)
            .execution_options(yield_per=200))
        async for sheet, number, state, errors in result:
            output = io.StringIO()
            csv.writer(output).writerow(["'" + sheet if sheet.lstrip().startswith(('=', '+', '-', '@')) else sheet,
                                        number, state, '; '.join(errors)])
            yield output.getvalue()
    async def generate():
        async with export_sessions() as export_db:
            async for chunk in rows(export_db):
                yield chunk
    return StreamingResponse(generate(), media_type='text/csv; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename="import-{import_id}-errors.csv"'})


class BatchControlInput(BaseModel):
    action: str = Field(pattern='^(pause|resume|retry|cancel)$')
    file_name_id: str | None = Field(default=None, max_length=200)
    reason: str = Field(min_length=1, max_length=2000)


@router.post('/{import_id}/control', status_code=202)
async def batch_control(import_id: UUID, payload: BatchControlInput, request: Request, db=Depends(get_db)):
    enabled()
    batch = await require_import(db, import_id, True)
    if batch.state not in {'REGISTERING', 'ACCEPTED', 'PAUSED', 'CANCELLED'}:
        raise HTTPException(409, '此批次尚未确认')
    if batch.control_action:
        raise HTTPException(409, '上一批次操作仍在执行，请刷新进度')
    batch.control_action, batch.control_after = payload.action, None
    batch.control_group, batch.control_actor, batch.control_reason = payload.file_name_id, get_principal(request).actor, payload.reason
    if payload.file_name_id is None:
        if payload.action in {'pause', 'cancel'}:
            batch.state = 'PAUSED' if payload.action == 'pause' else 'CANCELLED'
        elif payload.action == 'resume':
            pending = await db.scalar(select(func.count()).select_from(AttachmentImportRow).where(
                AttachmentImportRow.import_id == import_id, AttachmentImportRow.state == 'SELECTED'))
            batch.state = 'REGISTERING' if pending else 'ACCEPTED'
    row_query = update(AttachmentImportRow).where(AttachmentImportRow.import_id == import_id)
    if payload.file_name_id:
        row_query = row_query.where(AttachmentImportRow.normalized['file_name_id'].as_string() == payload.file_name_id)
    if payload.action in {'pause', 'cancel'}:
        await db.execute(row_query.where(AttachmentImportRow.state == 'SELECTED').values(
            state='SELECTED_PAUSED' if payload.action == 'pause' else 'SELECTED_CANCELLED'))
    elif payload.action == 'resume':
        await db.execute(row_query.where(AttachmentImportRow.state == 'SELECTED_PAUSED').values(state='SELECTED'))
        batch.state = 'REGISTERING'
    await db.commit()
    wakeup()
    return batch_dict(batch)
