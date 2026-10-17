"""附件工作队列与恢复；外部 IO 不占 SQL 事务，提交必须持有效下载凭证。"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select, text

from app.config import settings
from app.models.attachments import Attachment, AttachmentFileRevision, AttachmentImport, AttachmentVersion
from app.models.content import ContentBuildJob
from app.models.database import Document, ParseRun
from app.services.attachment_download import DownloadError, fetch_pdf
from app.services.attachment_import_service import process_preview, process_registration
from app.storage.minio_client import minio_client


async def claim_download(db, attachment_id, owner):
    config = settings.attachment_import
    # 串行化领取和全局并发计数，仅持有到事务提交。
    await db.execute(text('SELECT pg_advisory_xact_lock(753101501)'))
    now = datetime.utcnow()
    a = await db.get(Attachment, attachment_id, with_for_update=True, populate_existing=True)
    if not a or a.download_state not in {'QUEUED', 'FAILED_RETRYABLE', 'RUNNING'} or a.format != 'pdf':
        return None
    if a.download_state == 'RUNNING' and a.lease_until and a.lease_until > now:
        return None
    if a.next_attempt_at and a.next_attempt_at > now:
        return None
    if a.download_attempts >= config.max_attempts:
        a.download_state, a.last_error = 'FAILED_TERMINAL', 'DOWNLOAD_RETRIES_EXHAUSTED'
        return None
    running = await db.scalar(select(func.count()).select_from(Attachment).where(
        Attachment.download_state == 'RUNNING', Attachment.lease_until > now))
    if running >= config.download_concurrency:
        return None
    a.download_fence += 1
    a.download_attempts += 1
    a.download_state, a.lease_owner = 'RUNNING', owner
    a.lease_until = now + timedelta(seconds=config.lease_seconds)
    return dict(id=a.id, fence=a.download_fence, owner=owner, url=a.source_url)


def lease_valid(a, token):
    return bool(a and a.download_state == 'RUNNING' and a.download_fence == token['fence']
        and a.lease_owner == token['owner'] and a.lease_until and a.lease_until > datetime.utcnow())


async def download_attachment(sessions, attachment_id):
    async with sessions() as db, db.begin():
        token = await claim_download(db, attachment_id, str(uuid4()))
    if token is None:
        return
    loop = asyncio.get_running_loop()
    async def heartbeat():
        async with sessions() as db, db.begin():
            a = await db.get(Attachment, attachment_id, with_for_update=True, populate_existing=True)
            if not lease_valid(a, token):
                raise DownloadError('DOWNLOAD_LEASE_LOST')
            a.lease_until = datetime.utcnow() + timedelta(seconds=settings.attachment_import.lease_seconds)
    def heartbeat_sync():
        asyncio.run_coroutine_threadsafe(heartbeat(), loop).result(timeout=15)
    object_key = f'attachment-attempts/{attachment_id}/{token["fence"]}-{token["owner"]}.pdf'
    stored = False
    try:
        c = settings.attachment_import
        data, meta = await asyncio.to_thread(fetch_pdf, token['url'], sources=c.sources,
            max_bytes=c.max_download_bytes, timeout=c.download_timeout, heartbeat=heartbeat_sync)
        await heartbeat()
        # 每次尝试单独落对象。被拒绝的迟到提交只清理自己的对象。
        await asyncio.to_thread(minio_client.upload, object_key, data)
        stored = True
        async with sessions() as db, db.begin():
            a = await db.get(Attachment, attachment_id, with_for_update=True, populate_existing=True)
            if not lease_valid(a, token):
                return
            old = await db.get(AttachmentFileRevision, a.current_revision_id) if a.current_revision_id else None
            if old and old.sha256 == meta['sha256']:
                a.download_state, a.lease_until, a.lease_owner = 'SUCCEEDED', None, None
                return
            shared = await db.scalar(select(AttachmentFileRevision).where(
                AttachmentFileRevision.sha256 == meta['sha256'], AttachmentFileRevision.state == 'ACCEPTED')
                .order_by(AttachmentFileRevision.created_at).limit(1))
            key = shared.object_key if shared else object_key
            revision = AttachmentFileRevision(id=uuid4(), attachment_id=a.id, sha256=meta['sha256'],
                size=meta['size'], mime_type=meta['mime_type'], object_key=key,
                source_digest=meta['source_digest'], state='ID_CONTENT_CONFLICT' if old else 'ACCEPTED',
                metadata_snapshot=meta)
            db.add(revision)
            await db.flush()
            # 只有事务成功提交后才将尝试对象视为正式对象；提交异常必须保留清理机会。
            a.download_state, a.lease_until, a.lease_owner = 'SUCCEEDED', None, None
            if old:
                a.status, a.last_error = 'ID_CONTENT_CONFLICT', 'ATTACHMENT_ID_CONTENT_CONFLICT'
                return
            document = Document(document_id=uuid4(), file_name=a.attachment_name, original_name=a.attachment_name,
                file_path=key, file_size=meta['size'], file_hash=meta['sha256'], mime_type='application/pdf',
                mine_id=a.mine_code, mine_name=a.mine_name, checklist_type=a.checklist_type,
                report_period=a.report_time_raw, page_count=meta['page_count'], parse_status='PENDING')
            db.add(document)
            await db.flush()
            a.document_id, a.current_revision_id = document.document_id, revision.id
            a.status = 'READY' if a.checklist_type else 'WAITING_MAPPING'
            a.last_error = None
    except Exception as exc:
        async with sessions() as db, db.begin():
            a = await db.get(Attachment, attachment_id, with_for_update=True, populate_existing=True)
            if lease_valid(a, token):
                retryable = isinstance(exc, DownloadError) and exc.retryable
                a.download_state = 'FAILED_RETRYABLE' if retryable and a.download_attempts < settings.attachment_import.max_attempts else 'FAILED_TERMINAL'
                a.last_error = exc.code if isinstance(exc, DownloadError) else 'DOWNLOAD_OR_STORAGE_FAILED'
                a.next_attempt_at = datetime.utcnow() + timedelta(seconds=30 * 2 ** min(a.download_attempts, 6))
                a.lease_owner, a.lease_until = None, None
    finally:
        if stored:
            try:
                # 提交结果不确定时先核对数据库引用，绝不删除可能已提交的正式文件。
                async with sessions() as db:
                    reference = await db.scalar(select(AttachmentFileRevision.id).where(
                        AttachmentFileRevision.object_key == object_key).limit(1))
                if reference is None:
                    await asyncio.to_thread(minio_client.delete, object_key)
            except Exception:
                pass  # 数据库或存储不可用时保留对象，后续巡检再核对。


async def cleanup_attempt_objects(sessions, *, retention_seconds=86400, limit=200, after=None):
    """有界巡检；按对象键续扫，正式修订和仍有有效租约的尝试都保留。"""
    from itertools import islice
    cutoff = datetime.utcnow() - timedelta(seconds=retention_seconds)
    objects = await asyncio.to_thread(lambda: list(islice(minio_client.client.list_objects(
        minio_client.bucket, prefix='attachment-attempts/', recursive=True, start_after=after), limit)))
    removed = 0
    for obj in objects:
        if not obj.last_modified or obj.last_modified.replace(tzinfo=None) >= cutoff:
            continue
        try:
            attachment_id = UUID(obj.object_name.split('/')[1])
        except (ValueError, IndexError):
            continue
        async with sessions() as db, db.begin():
            a = await db.get(Attachment, attachment_id, with_for_update=True)
            reference = await db.scalar(select(AttachmentFileRevision.id).where(
                AttachmentFileRevision.object_key == obj.object_name).limit(1))
            if reference is not None or (a and a.lease_until and a.lease_until > datetime.utcnow()):
                continue
            await asyncio.to_thread(minio_client.delete, obj.object_name)
            removed += 1
    return {'removed': removed, 'next_after': objects[-1].object_name if len(objects) == limit else None}


async def reconcile_versions(sessions, limit=100):
    from app.services.attachment_version_service import finalize_version
    async with sessions() as db:
        ids = list((await db.scalars(select(AttachmentVersion.id).where(
            AttachmentVersion.state.in_(['QUEUED', 'RUNNING', 'PREPARING_CONTENT', 'PAUSED', 'WAITING_REUSE']))
            .order_by(AttachmentVersion.created_at).limit(limit))).all())
    for version_id in ids:
        async with sessions() as db, db.begin():
            v = await db.get(AttachmentVersion, version_id)
            if not v or v.snapshot is not None:
                continue
            if v.run_id:
                run = await db.get(ParseRun, v.run_id)
                if run:
                    await finalize_version(db, run)
            elif v.build_id:
                build = await db.get(ContentBuildJob, v.build_id)
                if build and build.dispatch_state in {'cancelled', 'superseded', 'failed_terminal'}:
                    v.state, v.last_error = 'FAILED', 'CONTENT_BUILD_FAILED'
                elif build and (build.checkpoint or {}).get('parse_run_id'):
                    run = await db.get(ParseRun, UUID(build.checkpoint['parse_run_id']))
                    if run:
                        await finalize_version(db, run)


async def start_ready(sessions, limit=20):
    from app.services.attachment_version_service import start_operation, VersionError
    if not settings.app.reliable_enabled:
        return
    async with sessions() as db:
        ids = list((await db.scalars(select(Attachment.id).where(Attachment.status == 'READY',
            Attachment.version_seq == 0, Attachment.document_id.is_not(None), Attachment.checklist_type.is_not(None))
            .order_by(Attachment.created_at).limit(limit))).all())
    for attachment_id in ids:
        async with sessions() as db, db.begin():
            await db.execute(text('SELECT pg_advisory_xact_lock(753101502)'))
            states = ['QUEUED', 'PREPARING_CONTENT', 'RUNNING', 'PAUSED']
            if settings.agent.reliable_quantum_queue_enabled:
                states.append('WAITING_REUSE')
            count = await db.scalar(select(func.count()).select_from(AttachmentVersion).where(
                AttachmentVersion.state.in_(states)))
            admission_limit = (settings.agent.reliable_inflight_documents
                if settings.agent.reliable_quantum_queue_enabled else settings.attachment_import.parse_concurrency)
            if count >= admission_limit:
                return
            try:
                async with db.begin_nested():
                    await start_operation(db, attachment_id, operation='INITIAL_EXTRACT',
                        request_id=f'initial:{attachment_id}', actor='system', reason='Excel 登记完成自动初提')
            except (VersionError, ValueError, RuntimeError) as exc:
                a = await db.get(Attachment, attachment_id)
                a.status, a.last_error = 'START_BLOCKED', 'NO_RULES_OR_CONFIGURATION_CONFLICT'


async def recover_imports(sessions, sender):
    """每次有界扫描。持久化状态即工作队列，漏发/重复唤醒均安全。"""
    if not settings.attachment_import.enabled:
        return {'status': 'disabled'}
    async with sessions() as db:
        controls = list((await db.scalars(select(AttachmentImport.id).where(
            AttachmentImport.control_action.is_not(None)).order_by(AttachmentImport.created_at).limit(5))).all())
        previews = list((await db.scalars(select(AttachmentImport.id).where(
            AttachmentImport.state == 'PREVIEW_QUEUED').order_by(AttachmentImport.created_at).limit(2))).all())
        registrations = list((await db.scalars(select(AttachmentImport.id).where(
            AttachmentImport.state == 'REGISTERING').order_by(AttachmentImport.created_at).limit(5))).all())
    for import_id in previews:
        await sender('preview', str(import_id))
    for import_id in controls:
        await process_batch_control(sessions, import_id)
    for import_id in registrations:
        await process_registration(sessions, import_id)
    now = datetime.utcnow()
    async with sessions() as db:
        downloads = list((await db.scalars(select(Attachment.id).where(Attachment.format == 'pdf', or_(
            and_(Attachment.download_state.in_(['QUEUED', 'FAILED_RETRYABLE']), or_(Attachment.next_attempt_at.is_(None), Attachment.next_attempt_at <= now)),
            and_(Attachment.download_state == 'RUNNING', Attachment.lease_until <= now)))
            .order_by(Attachment.created_at).limit(settings.attachment_import.download_concurrency * 2))).all())
    for attachment_id in downloads:
        await sender('download', str(attachment_id))
    await reconcile_versions(sessions)
    await start_ready(sessions)
    return {'preview_wakeups': len(previews), 'registration_batches': len(registrations), 'download_wakeups': len(downloads)}


async def process_batch_control(sessions, import_id, limit=100):
    from app.services.attachment_control_service import control_attachment
    from app.models.attachments import AttachmentImportRow
    from app.services.attachment_version_service import VersionError
    for _ in range(limit):
        async with sessions() as db, db.begin():
            batch = await db.get(AttachmentImport, import_id, with_for_update=True, populate_existing=True)
            if not batch.control_action:
                return
            query = select(AttachmentImportRow).where(AttachmentImportRow.import_id == import_id,
                AttachmentImportRow.attachment_id.is_not(None), AttachmentImportRow.state == 'REGISTERED')
            if batch.control_after:
                query = query.where(AttachmentImportRow.id > batch.control_after)
            if batch.control_group:
                query = query.where(AttachmentImportRow.normalized['file_name_id'].as_string() == batch.control_group)
            row = await db.scalar(query.order_by(AttachmentImportRow.id).limit(1))
            if row is None:
                batch.control_action = None
                return
            batch.control_after = row.id
            a0 = await db.get(Attachment, row.attachment_id)
            if a0.document_id:
                await db.get(Document, a0.document_id, with_for_update=True)
            a = await db.get(Attachment, row.attachment_id, with_for_update=True, populate_existing=True)
            if batch.control_action == 'retry':
                latest = await db.scalar(select(AttachmentVersion).where(AttachmentVersion.attachment_id == a.id)
                    .order_by(AttachmentVersion.number.desc()).limit(1))
                if a.download_state not in {'FAILED_TERMINAL', 'FAILED_RETRYABLE'} and a.status != 'START_BLOCKED' and (not latest or latest.state != 'FAILED'):
                    continue
            try:
                async with db.begin_nested():
                    await control_attachment(db, a, batch.control_action, actor=batch.control_actor, reason=batch.control_reason)
            except (VersionError, ValueError):
                # 终态版本不可暂停/取消；保留当前结果及下一行处理能力。
                continue
