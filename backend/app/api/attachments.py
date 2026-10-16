"""附件列表、版本快照和显式提取操作。"""
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import defer

from app.config import settings
from app.models.attachments import Attachment, AttachmentVersion, AttachmentVersionSummary
from app.models.content import ContentBuildJob, DocumentProcessingHead
from app.models.database import Document, ParseRun
from app.security import get_principal
from app.services.attachment_read_service import attachment_dict, attachment_page, snapshot_fact_view, version_dict
from app.services.attachment_version_service import VersionError, start_operation
from app.services.attachment_control_service import control_attachment
from app.storage import get_db

router = APIRouter(prefix='/api/v1/attachments', tags=['附件与结果版本'])


@router.get('')
async def list_attachments(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
    mine: str | None = None, checklist: str | None = None, file_name_id: str | None = None,
    period_type: str | None = None, period_key: str | None = None, name: str | None = None,
    status: str | None = None, download_state: str | None = None, latest_state: str | None = None,
    format: str | None = None, current_state: str | None = None, import_id: UUID | None = None, db=Depends(get_db)):
    return await attachment_page(db, page=page, page_size=page_size, mine=mine, checklist=checklist,
        file_name_id=file_name_id, period_type=period_type, period_key=period_key, name=name, status=status,
        download_state=download_state, import_id=import_id, latest_state=latest_state, format=format, current_state=current_state)


@router.get('/export')
async def export_attachments(mine: str | None = None, checklist: str | None = None,
    file_name_id: str | None = None, period_type: str | None = None, period_key: str | None = None,
    name: str | None = None, status: str | None = None, download_state: str | None = None,
    latest_state: str | None = None, format: str | None = None, current_state: str | None = None,
    import_id: UUID | None = None, db=Depends(get_db)):
    # 按主键顺序分页，仅导出当前已完成版本的统计，最近任务单列。
    export_sessions = async_sessionmaker(db.bind, expire_on_commit=False)
    async def rows(export_db):
        import csv
        import io
        yield '\ufeff附件ID,附件名称,煤矿编码,煤矿名称,清单ID,清单名称,所属期,格式,当前版本,字段目标数,可用字段数,匹配分,当前耗时秒,最近任务,首次登记,当前结果时间\r\n'
        page = 1
        while True:
            result = await attachment_page(export_db, page=page, page_size=100, mine=mine, checklist=checklist,
                file_name_id=file_name_id, period_type=period_type, period_key=period_key, name=name,
                status=status, download_state=download_state, latest_state=latest_state, format=format,
                current_state=current_state, import_id=import_id)
            if not result['items']:
                break
            for item in result['items']:
                current, latest = item['current'] or {}, item['latest'] or {}
                stats = current.get('stats') or {}
                values = [item['source_id'], item['attachment_name'], item['mine_code'], item['mine_name'],
                    item['file_name_id'], item['file_name'], item['report_time_raw'], item['format'],
                    current.get('number', ''), stats.get('targets', ''), stats.get('available', ''),
                    stats.get('mean_confidence', ''), current.get('duration_seconds', ''), latest.get('state', ''),
                    item['created_at'], current.get('completed_at', '')]
                output = io.StringIO()
                csv.writer(output).writerow(["'" + str(value) if str(value).lstrip().startswith(('=', '+', '-', '@'))
                    else '' if value is None else str(value) for value in values])
                yield output.getvalue()
            page += 1
    async def generate():
        async with export_sessions() as export_db, export_db.begin():
            await export_db.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ'))
            async for chunk in rows(export_db):
                yield chunk
    return StreamingResponse(generate(), media_type='text/csv; charset=utf-8',
        headers={'Content-Disposition': 'attachment; filename="attachments-current.csv"'})


async def require_attachment(db, attachment_id, lock=False):
    a = await db.get(Attachment, attachment_id, with_for_update=lock, populate_existing=True)
    if not a:
        raise HTTPException(404, '附件不存在')
    return a


def _version_query():
    return select(AttachmentVersion, AttachmentVersionSummary).outerjoin(
        AttachmentVersionSummary, AttachmentVersionSummary.version_id == AttachmentVersion.id
    ).options(defer(AttachmentVersion.snapshot))


@router.get('/{attachment_id}')
async def attachment_detail(attachment_id: UUID, db=Depends(get_db)):
    a = await require_attachment(db, attachment_id)
    current = (await db.execute(_version_query().where(AttachmentVersion.id == a.current_version_id))).one_or_none() if a.current_version_id else None
    latest = (await db.execute(_version_query().where(AttachmentVersion.attachment_id == a.id)
        .order_by(AttachmentVersion.number.desc()).limit(1))).one_or_none()
    head = await db.get(DocumentProcessingHead, a.document_id) if a.document_id else None
    current_version, current_summary = current or (None, None)
    latest_version, latest_summary = latest or (None, None)
    return attachment_dict(a, current_version, latest_version, head, current_summary, latest_summary)


@router.get('/{attachment_id}/versions')
async def versions(attachment_id: UUID, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100), db=Depends(get_db)):
    a = await require_attachment(db, attachment_id)
    total = await db.scalar(select(func.count()).select_from(AttachmentVersion).where(AttachmentVersion.attachment_id == a.id))
    rows = (await db.execute(_version_query().where(AttachmentVersion.attachment_id == a.id)
        .order_by(AttachmentVersion.number.desc()).offset((page - 1) * page_size).limit(page_size))).all()
    return {'total': total, 'current_version_id': a.current_version_id,
            'items': [version_dict(version, summary=summary) for version, summary in rows]}


async def require_version(db, attachment_id, version_id):
    await require_attachment(db, attachment_id)
    v = await db.get(AttachmentVersion, version_id)
    if not v or v.attachment_id != attachment_id:
        raise HTTPException(404, '附件版本不存在')
    return v


@router.get('/{attachment_id}/versions/{version_id}')
async def version_detail(attachment_id: UUID, version_id: UUID, db=Depends(get_db)):
    v = await require_version(db, attachment_id, version_id)
    return version_dict(v, include_snapshot=True)


@router.get('/{attachment_id}/current/facts')
async def current_facts(attachment_id: UUID, view: str = Query('automatic', pattern='^(automatic|reviewed|available)$'), db=Depends(get_db)):
    a = await require_attachment(db, attachment_id)
    if not a.current_version_id:
        raise HTTPException(409, '尚无完整结果版本')
    v = await db.get(AttachmentVersion, a.current_version_id)
    return await snapshot_fact_view(db, v, view=view, overlay=True)


@router.get('/{attachment_id}/versions/{version_id}/export')
async def export_version(attachment_id: UUID, version_id: UUID, db=Depends(get_db)):
    import json
    v = await require_version(db, attachment_id, version_id)
    if not v.snapshot:
        raise HTTPException(409, '此版本尚无完整快照')
    return Response(json.dumps(v.snapshot, ensure_ascii=False), media_type='application/json',
        headers={'Content-Disposition': f'attachment; filename="attachment-{attachment_id}-V{v.number}.json"'})


class OperationInput(BaseModel):
    operation: str = Field(pattern='^(INITIAL_EXTRACT|INCREMENTAL_EXTRACT|FULL_REEXTRACT)$')
    request_id: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=2000)


@router.post('/{attachment_id}/operations', status_code=202)
async def operation(attachment_id: UUID, payload: OperationInput, request: Request, db=Depends(get_db)):
    if not settings.attachment_import.enabled or not settings.app.reliable_enabled:
        raise HTTPException(409, '附件写入口或可靠解析尚未启用')
    try:
        v = await start_operation(db, attachment_id, operation=payload.operation, request_id=payload.request_id,
            actor=get_principal(request).actor, reason=payload.reason)
    except (VersionError, ValueError, RuntimeError) as exc:
        await db.rollback()
        raise HTTPException(409, str(exc)) from exc
    await db.commit()
    from app.api.attachment_imports import wakeup
    wakeup()
    return version_dict(v)


class ControlInput(BaseModel):
    action: str = Field(pattern='^(pause|resume|cancel|retry)$')
    reason: str = Field(min_length=1, max_length=2000)


@router.post('/{attachment_id}/control')
async def control(attachment_id: UUID, payload: ControlInput, request: Request, db=Depends(get_db)):
    a0 = await require_attachment(db, attachment_id)
    if a0.document_id:
        await db.get(Document, a0.document_id, with_for_update=True)
    a = await require_attachment(db, attachment_id, True)
    try:
        await control_attachment(db, a, payload.action, actor=get_principal(request).actor, reason=payload.reason)
    except (VersionError, ValueError, RuntimeError) as exc:
        await db.rollback()
        raise HTTPException(409, str(exc)) from exc
    await db.commit()
    from app.api.attachment_imports import wakeup
    wakeup()
    return await attachment_detail(attachment_id, db)


@router.get('/operations/metrics/summary')
async def metrics(db=Depends(get_db)):
    from app.models.attachments import AttachmentImport
    counts = dict((await db.execute(select(Attachment.download_state, func.count()).group_by(Attachment.download_state))).all())
    versions = dict((await db.execute(select(AttachmentVersion.state, func.count()).group_by(AttachmentVersion.state))).all())
    oldest = await db.scalar(select(func.min(Attachment.created_at)).where(Attachment.download_state.in_(['QUEUED', 'FAILED_RETRYABLE'])))
    return {'download': counts, 'versions': versions, 'oldest_download_wait_at': oldest,
        'expired_download_leases': await db.scalar(select(func.count()).select_from(Attachment).where(
            Attachment.download_state == 'RUNNING', Attachment.lease_until < datetime.utcnow())),
        'imports_pending': await db.scalar(select(func.count()).select_from(AttachmentImport).where(
            AttachmentImport.state.in_(['PREVIEW_QUEUED', 'REGISTERING'])))}
