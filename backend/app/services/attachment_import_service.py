"""Excel 暂存、确认与共享附件登记入口；每行独立提交，可重启恢复。"""
from __future__ import annotations

import hashlib
from collections import Counter
from datetime import datetime
from uuid import uuid4

from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert

from app.config import settings
from app.models.attachments import Attachment, AttachmentChecklistMapping, AttachmentImport, AttachmentImportRow
from app.services.attachment_excel import ExcelError, iter_excel
from app.services.attachment_input import validate_attachment_data


class AttachmentConflict(ValueError):
    pass


async def identity_lock(db, namespace: str, source_id: str):
    # 缺失行也需要加锁；唯一约束是最终防线。固定哈希避免 Python hash 随进程变化。
    key = int.from_bytes(hashlib.sha256(f'{namespace}\0{source_id}'.encode()).digest()[:8], 'big', signed=True)
    await db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': key})


def public_metadata(a: Attachment):
    return {**{key: getattr(a, key) for key in ('source_system', 'source_id', 'mine_code', 'mine_name',
        'file_name_id', 'file_name', 'report_time_raw', 'update_rate', 'cycle', 'period_type', 'period_key',
        'normalization_status', 'attachment_name', 'format', 'checklist_type')},
        'attachment_link': a.source_url}


async def register_attachment(db, *, source_system: str, row: dict, checklist_type: str | None):
    """供 Excel 和未来适配器共用；重复登记不触发下载或模型重跑。"""
    row, errors = validate_attachment_data(row)
    if errors:
        raise AttachmentConflict('INVALID_ATTACHMENT_DATA：' + '；'.join(errors))
    await identity_lock(db, source_system, row['attachment_id'])
    a = await db.scalar(select(Attachment).where(Attachment.source_system == source_system,
        Attachment.source_id == row['attachment_id']))
    # 与提取/控制相同：先 Document 后 Attachment，身份锁仅串行化同来源登记。
    if a and a.document_id:
        from app.models.database import Document
        await db.get(Document, a.document_id, with_for_update=True)
    if a:
        a = await db.get(Attachment, a.id, with_for_update=True, populate_existing=True)
    previous = public_metadata(a) if a else None
    if a:
        scope = (a.mine_code, a.file_name_id, a.report_time_raw, a.update_rate, a.cycle)
        incoming = tuple(row[key] for key in ('mine_code', 'file_name_id', 'report_time', 'update_rate', 'cycle'))
        if scope != incoming or (checklist_type and a.checklist_type and a.checklist_type != checklist_type):
            raise AttachmentConflict('SOURCE_SCOPE_CONFLICT：同一附件 ID 的煤矿/清单/周期或映射冲突')
        a.mine_name, a.file_name = row['mine_name'], row['file_name']
        a.attachment_name, a.source_url = row['attachment_name'], row['attachment_link']
        # 已下载的文件以检测类型为准，名称变化不能把它变成另一个文件。
        if a.current_revision_id is None:
            a.format = row['format']
            if a.format != 'pdf':
                a.download_state = 'TYPE_UNSUPPORTED'
            if a.download_state == 'TYPE_UNSUPPORTED' and a.format == 'pdf':
                a.download_state = 'QUEUED'
        if checklist_type and not a.checklist_type:
            a.checklist_type = checklist_type
            if a.document_id:
                from app.models.database import Document
                document = await db.get(Document, a.document_id)
                if document is not None:
                    document.checklist_type = checklist_type
            if a.current_revision_id:
                a.status = 'READY'
        return a, 'REUSED', {'before': previous, 'after': public_metadata(a)}
    a = Attachment(id=uuid4(), source_system=source_system, source_id=row['attachment_id'],
        mine_code=row['mine_code'], mine_name=row['mine_name'], file_name_id=row['file_name_id'], file_name=row['file_name'],
        report_time_raw=row['report_time'], update_rate=row['update_rate'], cycle=row['cycle'],
        period_type=row['period_type'], period_key=row['period_key'], normalization_status=row['normalization_status'],
        attachment_name=row['attachment_name'], source_url=row['attachment_link'], format=row['format'],
        checklist_type=checklist_type, status='REGISTERED' if checklist_type else 'WAITING_MAPPING',
        download_state='QUEUED' if row['format'] == 'pdf' else 'TYPE_UNSUPPORTED')
    db.add(a)
    await db.flush()
    return a, 'REGISTERED', {'after': public_metadata(a)}


async def process_preview(sessions, import_id):
    # 暂存事务独占预览；分块插入有界，崩溃回滚后重新校验，不持有业务附件锁。
    async with sessions() as db:
        locked = await db.scalar(text('SELECT pg_try_advisory_xact_lock(:key)'),
                                 {'key': int(import_id.int % (2**63 - 1))})
        if not locked:
            return
        try:
            batch = await db.get(AttachmentImport, import_id, with_for_update=True)
            if batch is None or batch.state != 'PREVIEW_QUEUED':
                return
            data = batch.staging_file
            await db.execute(delete(AttachmentImportRow).where(AttachmentImportRow.import_id == import_id))
            seen, chunk, counts = set(), [], Counter()
            for sheet, number, raw, row, errors in iter_excel(data,
                    max_rows=settings.attachment_import.max_rows,
                    max_expanded_bytes=settings.attachment_import.max_expanded_bytes):
                if row['attachment_id'] in seen:
                    errors = [*errors, 'DUPLICATE_ROW：本文件内附件 ID 重复']
                seen.add(row['attachment_id'])
                state = 'INVALID' if errors else 'VALID'
                counts[state] += 1
                raw, row = dict(raw), dict(row)
                chunk.append(dict(id=uuid4(), import_id=import_id, sheet=sheet, row_number=number,
                                  raw=raw, normalized=row, source_url=row['attachment_link'], errors=errors, state=state))
                if len(chunk) == 500:
                    await db.execute(insert(AttachmentImportRow), chunk)
                    await db.flush()
                    chunk = []
            if chunk:
                await db.execute(insert(AttachmentImportRow), chunk)
            batch = await db.get(AttachmentImport, import_id, populate_existing=True)
            batch.counts, batch.state = dict(counts), 'PREVIEW_READY'
            batch.staging_file = None
            checklist_ids = select(AttachmentImportRow.normalized['file_name_id'].as_string()).where(AttachmentImportRow.import_id == import_id).distinct()
            mappings = (await db.scalars(select(AttachmentChecklistMapping).where(AttachmentChecklistMapping.source_system == batch.source_system, AttachmentChecklistMapping.file_name_id.in_(checklist_ids)))).all()
            batch.mappings = {m.file_name_id: m.checklist_type for m in mappings}
            # 成功预览后删除暂存 Excel；原始行和业务链接已写入导入记录。
            await db.commit()
        except Exception as exc:
            await db.rollback()
            batch = await db.get(AttachmentImport, import_id)
            if batch:
                batch.state = 'PREVIEW_FAILED'
                batch.staging_file = None
                batch.last_error = str(exc)[:300] if isinstance(exc, ExcelError) else 'EXCEL_INVALID_ARCHIVE'
                await db.commit()


async def process_registration(sessions, import_id, *, limit=250):
    processed = 0
    for _ in range(limit):
        async with sessions() as db, db.begin():
            batch = await db.get(AttachmentImport, import_id, with_for_update=True)
            if batch is None or batch.state != 'REGISTERING':
                break
            item = await db.scalar(select(AttachmentImportRow).where(AttachmentImportRow.import_id == import_id,
                AttachmentImportRow.state == 'SELECTED').order_by(AttachmentImportRow.row_number)
                .with_for_update(skip_locked=True).limit(1))
            if item is None:
                # SELECTED 可能正被其他 worker 领取；由后续恢复重新汇总。
                remaining = await db.scalar(select(func.count()).select_from(AttachmentImportRow).where(
                    AttachmentImportRow.import_id == import_id, AttachmentImportRow.state == 'SELECTED'))
                if not remaining:
                    batch.state = 'ACCEPTED'
                break
            mapping = batch.mappings.get(item.normalized['file_name_id'])
            try:
                async with db.begin_nested():
                    a, state, observation = await register_attachment(db, source_system=batch.source_system,
                        row=item.normalized, checklist_type=mapping)
                item.attachment_id, item.state, item.observation = a.id, state, observation
            except AttachmentConflict as exc:
                item.state, item.errors = 'CONFLICT', [str(exc)]
            processed += 1
    return processed
