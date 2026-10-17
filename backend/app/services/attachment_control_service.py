"""附件及其当前工作项控制；调用方按 Document、Attachment 顺序加锁。"""
from datetime import datetime

from sqlalchemy import select

from app.models.attachments import AttachmentVersion
from app.models.content import ContentBuildJob
from app.services.attachment_version_service import VersionError


async def control_attachment(db, a, action, *, actor, reason):
    from app.services.reliable_extraction_service import reliable_extraction_service
    # 调用方已经按 Document -> Attachment 加锁。
    v = await db.scalar(select(AttachmentVersion).where(AttachmentVersion.attachment_id == a.id)
        .order_by(AttachmentVersion.number.desc()).with_for_update().limit(1))
    if v and not v.snapshot:
        shared_waiting = None
        if v.run_id:
            from app.models.database import ParseRun
            shared_run = await db.get(ParseRun, v.run_id)
            if (shared_run.extraction_profile or {}).get('shared_execution_id'):
                from app.models.shared_execution import SharedExecutionSubscriber
                shared_waiting = await db.scalar(select(SharedExecutionSubscriber).where(
                    SharedExecutionSubscriber.target_version_id == v.id,
                    SharedExecutionSubscriber.state.in_(['WAITING', 'CANCELLED'])).with_for_update())
        if shared_waiting:
            from app.models.database import ParseRun
            from app.models.execution import ParseFieldJob
            run = await db.get(ParseRun, v.run_id, with_for_update=True)
            if shared_waiting.state == 'CANCELLED' and action != 'cancel':
                raise VersionError('SHARED_TARGET_CANCELLED：请创建新的提取操作')
            if action == 'cancel':
                shared_waiting.state = 'CANCELLED'
                run.status = v.state = 'CANCELLED'
                run.execution_generation += 1
                for job in (await db.scalars(select(ParseFieldJob).where(
                        ParseFieldJob.run_id == run.parse_run_id).with_for_update())).all():
                    job.execution_state, job.phase = 'cancelled', 'cancelled'
                    job.fence_token += 1
            elif action == 'pause':
                run.status = v.state = 'PAUSED'
            elif action in {'resume', 'retry'}:
                run.status = v.state = 'WAITING_REUSE'
        elif v.run_id:
            await reliable_extraction_service.control(db, v.run_id, 'resume' if action == 'retry' else action,
                                                       retry_failed=action == 'retry')
            v.state = {'pause': 'PAUSED', 'resume': 'RUNNING', 'retry': 'RUNNING', 'cancel': 'CANCELLED'}[action]
        elif v.build_id:
            build = await db.get(ContentBuildJob, v.build_id, with_for_update=True)
            if action == 'cancel':
                build.dispatch_state, build.fence_token = 'cancelled', build.fence_token + 1
                v.state = 'CANCELLED'
            elif action == 'pause':
                build.dispatch_state, build.fence_token = 'paused', build.fence_token + 1
                v.state = 'PAUSED'
            elif action in {'resume', 'retry'}:
                if build.dispatch_state in {'cancelled', 'superseded', 'failed_terminal'}:
                    raise VersionError('CONTENT_BUILD_TERMINAL：请创建新的全量提取操作')
                build.dispatch_state = 'queued'
                build.fence_token += 1
                build.lease_until, build.lease_owner = None, None
                from app.services.execution import JobRepository
                await JobRepository(db).enqueue_outbox(kind='content.build.execute', aggregate_id=build.build_id,
                    dedupe_key=f'attachment-content:{v.id}:{build.fence_token}', payload={'build_id': str(build.build_id)})
                v.state = 'PREPARING_CONTENT'
        v.last_error = None
    if a.download_state != 'SUCCEEDED' and a.download_state != 'TYPE_UNSUPPORTED':
        a.download_fence += 1
        a.lease_owner, a.lease_until = None, None
        a.download_state = {'pause': 'PAUSED', 'resume': 'QUEUED', 'retry': 'QUEUED', 'cancel': 'CANCELLED'}[action]
        if action == 'retry':
            a.download_attempts, a.last_error = 0, None
        a.next_attempt_at = None
    if action in {'pause', 'cancel'} and a.status != 'ID_CONTENT_CONFLICT':
        a.status = 'PAUSED' if action == 'pause' else 'CANCELLED'
    elif action in {'resume', 'retry'} and a.status in {'PAUSED', 'START_BLOCKED', 'CANCELLED'}:
        a.status = 'READY' if a.current_revision_id and a.checklist_type else 'REGISTERED'
    # 追加操作审计到既有 outbox，不改写已完成快照。
    from app.services.execution import JobRepository
    from uuid import uuid4
    await JobRepository(db).enqueue_outbox(kind='attachment.audit', aggregate_id=a.id, dedupe_key=str(uuid4()),
        payload={'action': action, 'actor': actor, 'reason': reason, 'at': datetime.utcnow().isoformat()})


