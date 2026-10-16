"""一次提取一个版本；完整执行后冻结，CAS 切换当前，重试沿用原身份。"""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import select

from app.config import settings
from app.models.attachments import Attachment, AttachmentFileRevision, AttachmentVersion, AttachmentVersionSummary
from app.models.content import ContentBuildJob, DocumentProcessingHead
from app.models.database import Document, ExtractedFieldModel, ParseRun
from app.models.execution import ParseFieldJob
from app.models.reviews import FieldReviewRevision
from app.services.attachment_import_service import public_metadata
from app.services.attachment_read_service import summary_from_snapshot

ACTIVE_VERSION_STATES = {'QUEUED', 'PREPARING_CONTENT', 'RUNNING', 'PAUSED'}


class VersionError(ValueError):
    pass


def json_value(value):
    return json.loads(json.dumps(value, default=str, ensure_ascii=False))


def digest(value):
    return hashlib.sha256(json.dumps(value, default=str, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def safe_config(value):
    """保留规则和模型名称，移除认证及 URL 查询材料。"""
    from urllib.parse import urlsplit, urlunsplit
    if isinstance(value, dict):
        return {key: safe_config(val) for key, val in value.items()
                if not any(x in key.lower() for x in ('api_key', 'authorization', 'password', 'secret', 'source_url', 'attachment_link'))}
    if isinstance(value, list):
        return [safe_config(item) for item in value]
    if isinstance(value, str) and '://' in value:
        try:
            p = urlsplit(value)
            if p.scheme.lower() not in {'http', 'https'} or not p.hostname:
                return '[INVALID_SOURCE_URL]'
            host = p.hostname
            host = '[' + host + ']' if ':' in host else host
            if p.port:
                host += ':' + str(p.port)
            return urlunsplit((p.scheme.lower(), host, p.path, '', ''))
        except (ValueError, UnicodeError):
            return '[INVALID_SOURCE_URL]'
    return value


def model_signature():
    model = settings.models['vision_llm']
    return digest({'base_url': safe_config(model.base_url), 'model_name': model.model_name,
        'model_version': model.model_version, 'thinking_enabled': settings.agent.thinking_enabled,
        'strategy': settings.agent.reliable_strategy})


async def managed_attachment(db, document_id, *, lock=False):
    q = select(Attachment).where(Attachment.document_id == document_id)
    if lock:
        q = q.with_for_update().execution_options(populate_existing=True)
    return await db.scalar(q)


async def ensure_manual_attachment(db, document):
    a = await managed_attachment(db, document.document_id, lock=True)
    if a:
        return a
    a = Attachment(id=uuid4(), source_system='manual', source_id=str(document.document_id),
        mine_code=document.mine_id or '', mine_name=document.mine_name or '', file_name_id=document.checklist_type or '',
        file_name=document.checklist_type or '', report_time_raw=document.report_period or '', update_rate=0, cycle='',
        period_type='unknown', period_key=document.report_period or '', normalization_status='RAW_ONLY',
        attachment_name=document.original_name, source_url='', format='pdf', checklist_type=document.checklist_type,
        document_id=document.document_id, status='READY', download_state='SUCCEEDED')
    db.add(a)
    await db.flush()
    revision = AttachmentFileRevision(id=uuid4(), attachment_id=a.id, sha256=document.file_hash,
        size=document.file_size, mime_type=document.mime_type, object_key=document.file_path,
        source_digest=digest('manual'), state='ACCEPTED', metadata_snapshot={'page_count': document.page_count})
    db.add(revision)
    await db.flush()
    a.current_revision_id = revision.id
    return a


async def create_version(db, a, *, operation, request_id, actor, reason, inherited_fields=()):
    old = await db.scalar(select(AttachmentVersion).where(AttachmentVersion.attachment_id == a.id,
        AttachmentVersion.request_id == request_id))
    if old:
        if old.operation != operation:
            raise VersionError('REQUEST_ID_CONFLICT')
        return old
    active = await db.scalar(select(AttachmentVersion.id).where(AttachmentVersion.attachment_id == a.id,
        AttachmentVersion.state.in_(ACTIVE_VERSION_STATES)).limit(1))
    if active:
        raise VersionError('ACTIVE_OPERATION：附件已有活动提取任务')
    if not a.current_revision_id or not a.document_id:
        raise VersionError('FILE_NOT_READY')
    a.version_seq += 1
    v = AttachmentVersion(id=uuid4(), attachment_id=a.id, number=a.version_seq, request_id=request_id,
        operation=operation, parent_id=a.current_version_id, expected_cas=a.cas_version,
        file_revision_id=a.current_revision_id, actor=actor, reason=reason, state='QUEUED',
        inherited_fields=deepcopy(list(inherited_fields)))
    db.add(v)
    await db.flush()
    return v


async def bind_start(db, document, profile, version_id=None):
    """由可靠引擎调用；直接人工可靠提取也获得相同的版本门禁。Document 已锁定。"""
    a = await ensure_manual_attachment(db, document)
    if version_id:
        v = await db.get(AttachmentVersion, version_id, with_for_update=True)
        if not v or v.attachment_id != a.id or v.state not in {'QUEUED', 'PREPARING_CONTENT'}:
            raise VersionError('INVALID_VERSION_OPERATION')
    else:
        if a.source_system != 'manual' or a.version_seq:
            raise VersionError('请从附件版本操作入口发起补提或全量重提')
        v = await create_version(db, a, operation='INITIAL_EXTRACT',
            request_id=profile.get('request_id') or str(uuid4()), actor='system', reason='人工上传初提')
    profile['attachment_version_id'] = str(v.id)
    profile['attachment_model_signature'] = model_signature()
    return v


async def start_operation(db, attachment_id, *, operation, request_id, actor, reason):
    from app.rules.repository import rule_repository
    from app.services.reliable_extraction_service import ContentPreparationRequired, reliable_extraction_service
    # 所有路径按 Document -> Attachment -> Version -> Head 的顺序加锁。
    a0 = await db.get(Attachment, attachment_id)
    if not a0 or not a0.document_id:
        raise VersionError('FILE_NOT_READY')
    document = await db.get(Document, a0.document_id, with_for_update=True, populate_existing=True)
    a = await managed_attachment(db, document.document_id, lock=True)
    if a.status == 'ID_CONTENT_CONFLICT':
        raise VersionError('ID_CONTENT_CONFLICT')
    if operation not in {'INITIAL_EXTRACT', 'INCREMENTAL_EXTRACT', 'FULL_REEXTRACT'}:
        raise VersionError('INVALID_OPERATION')
    existing = await db.scalar(select(AttachmentVersion).where(AttachmentVersion.attachment_id == a.id,
        AttachmentVersion.request_id == request_id))
    if existing:
        if existing.operation != operation:
            raise VersionError('REQUEST_ID_CONFLICT')
        return existing
    if operation == 'INITIAL_EXTRACT' and a.version_seq:
        raise VersionError('INITIAL_ALREADY_REQUESTED')
    parent = await db.get(AttachmentVersion, a.current_version_id) if a.current_version_id else None
    rules = await rule_repository.get_rules_for_document(db, document)
    inherited = []
    if operation == 'INCREMENTAL_EXTRACT':
        if not parent or not parent.snapshot:
            raise VersionError('NO_CURRENT_SNAPSHOT')
        from app.services.content_publish_service import content_publish_service
        run = await db.get(ParseRun, parent.run_id)
        await content_publish_service.guard_revision(db, document_id=document.document_id,
            revision_id=run.content_revision_id,
            expected_invalidation_watermark=(run.extraction_profile or {}).get('invalidation_watermark'))
        snapshot = parent.snapshot
        if snapshot.get('model_signature') != model_signature():
            raise VersionError('MODEL_CHANGED：模型或策略变化，请全量重提')
        hashes = {r.field_key: r.rule_semantic_hash for r in rules}
        previous = snapshot['rule_hashes']
        if any(key not in hashes or hashes[key] != value for key, value in previous.items()):
            raise VersionError('RULE_CHANGED：原目标已变化或删除，请全量重提')
        inherited = deepcopy(snapshot['fields'])
        for item in inherited:
            item['inherited_from_version_id'] = str(parent.id)
        rules = [r for r in rules if r.field_key not in previous]
        if not rules:
            raise VersionError('NO_ADDED_TARGETS')
    v = await create_version(db, a, operation=operation, request_id=request_id, actor=actor, reason=reason,
                             inherited_fields=inherited)
    try:
        run = await reliable_extraction_service.start(db, document.document_id, rules=rules,
            parent_run_id=parent.run_id if parent else None, request_id=f'attachment:{v.id}', attachment_version_id=v.id)
        v.run_id, v.state = run.parse_run_id, 'RUNNING'
        run.run_mode = 'INCREMENTAL' if operation == 'INCREMENTAL_EXTRACT' else 'FULL'
    except ContentPreparationRequired as exc:
        if operation == 'FULL_REEXTRACT' and parent:
            # 已有结果的全量重提不得悄悄重新 OCR；调用事务回滚内容准备。
            raise VersionError('CONTENT_INCOMPATIBLE：需要单独内容重建操作') from exc
        v.build_id, v.state = exc.build_id, 'PREPARING_CONTENT'
    return v


def flow_state(run, jobs):
    from app.services.execution.field_scheduler import aggregate_run_status

    if run.status in {'CANCELLED', 'SUPERSEDED', 'PAUSED'}:
        return run.status
    if run.status == 'FAILED':
        return 'FAILED'
    if any(j.execution_state in {'cancelled', 'superseded'} for j in jobs):
        return 'INCOMPLETE'
    # 复用可靠执行器的完整性契约；done + incomplete 也不能覆盖当前结果。
    status = aggregate_run_status(run, jobs)
    if status == 'FAILED':
        return 'FAILED'
    if jobs and status in {'COMPLETED', 'COMPLETED_WITH_REVIEW'}:
        return 'COMPLETED'
    return 'RUNNING'


def field_stats(fields):
    counts = dict(targets=len(fields), completed_search=0, found=0, available=0, review_required=0, not_found=0, incomplete=0)
    scores = []
    methods = set()
    for item in fields:
        state = item['semantic_state']
        counts['completed_search'] += state in {'completed', 'review_required', 'not_found'}
        counts['review_required'] += state == 'review_required'
        counts['not_found'] += state == 'not_found'
        counts['incomplete'] += state == 'incomplete'
        facts = (item.get('facts') or {}).get('automatic', [])
        counts['found'] += bool(facts)
        counts['available'] += any(f.get('available_for_downstream') for f in facts)
        field = item.get('field') or {}
        if field.get('confidence') is not None:
            scores.append(field['confidence'])
            methods.add(field.get('extraction_method'))
    # 不混合未经校准的不同方法分数。
    counts['mean_confidence'] = sum(scores) / len(scores) if scores and len(methods) == 1 else None
    counts['score_count'] = len(scores)
    counts['score_policy'] = 'same_method_non_null_mean'
    return counts


async def finalize_version(db, run):
    profile = run.extraction_profile or {}
    version_id = profile.get('attachment_version_id')
    if not version_id:
        return False
    document = await db.get(Document, run.document_id, with_for_update=True, populate_existing=True)
    a = await managed_attachment(db, run.document_id, lock=True)
    v = await db.get(AttachmentVersion, UUID(version_id), with_for_update=True, populate_existing=True)
    if not a or not v or v.snapshot is not None:
        return True
    if v.state == 'CANCELLED':
        return True
    await db.get(DocumentProcessingHead, run.document_id, with_for_update=True)
    run = await db.get(ParseRun, run.parse_run_id, with_for_update=True, populate_existing=True)
    v.run_id = run.parse_run_id
    jobs = list((await db.scalars(select(ParseFieldJob).where(ParseFieldJob.run_id == run.parse_run_id))).all())
    state = flow_state(run, jobs)
    v.state = state
    v.last_error = ('EXTRACTION_FAILED：请查看执行记录' if state == 'FAILED' else None)
    if state != 'COMPLETED':
        return True
    from app.services.content_publish_service import content_publish_service
    try:
        head = await content_publish_service.guard_revision(db, document_id=run.document_id,
            revision_id=run.content_revision_id, expected_invalidation_watermark=profile.get('invalidation_watermark'))
    except RuntimeError as exc:
        from app.services.content_publish_service import ContentPublicationError
        # 不吞掉数据库或编程故障；内容门禁拒绝是可解释的业务终态。
        if not isinstance(exc, ContentPublicationError):
            raise
        v.state, v.last_error = 'INCOMPLETE', 'CONTENT_INVALIDATED_OR_CHANGED'
        return True
    if a.cas_version != v.expected_cas or a.current_version_id != v.parent_id or a.current_revision_id != v.file_revision_id:
        v.state, v.last_error = 'SUPERSEDED', 'PARENT_SNAPSHOT_CHANGED'
        return True
    fields = list((await db.scalars(select(ExtractedFieldModel).where(
        ExtractedFieldModel.parse_run_id == run.parse_run_id))).all())
    reviews = list((await db.scalars(select(FieldReviewRevision).where(
        FieldReviewRevision.parse_run_id == run.parse_run_id))).all())
    from app.services.fact_service import build_fact_view_with_invalidations
    auto = await build_fact_view_with_invalidations(db, run, fields, view='automatic', revisions=reviews)
    reviewed = await build_fact_view_with_invalidations(db, run, fields, view='reviewed', revisions=reviews)
    rows = deepcopy(v.inherited_fields or [])
    # 补提定版时冻结继承字段最新复核；父历史快照不变。
    for item in rows:
        inherited_run = await db.get(ParseRun, UUID(item['run_id']))
        inherited_reviews = list((await db.scalars(select(FieldReviewRevision).where(
            FieldReviewRevision.parse_run_id == inherited_run.parse_run_id,
            FieldReviewRevision.field_key == item['field_key']))).all())
        if item.get('field'):
            inherited_view = await build_fact_view_with_invalidations(db, inherited_run,
                [item['field']], view='reviewed', revisions=inherited_reviews)
            item['facts']['reviewed'] = [f.model_dump(mode='json') for f in inherited_view.facts]
        reviews.extend(inherited_reviews)
    by_key = {f.field_key: f for f in fields}
    for job in jobs:
        field = by_key.get(job.field_key)
        rows.append({'field_key': job.field_key, 'semantic_state': job.semantic_state,
            'run_id': str(run.parse_run_id), 'rule_hash': job.rule_hash, 'rule': safe_config(job.checkpoint.get('rule_snapshot') or {}),
            'field': json_value({c.name: getattr(field, c.name) for c in field.__table__.columns}) if field else None,
            'facts': {'automatic': [f.model_dump(mode='json') for f in auto.facts if f.field_key == job.field_key],
                      'reviewed': [f.model_dump(mode='json') for f in reviewed.facts if f.field_key == job.field_key]}})
    carried = 0
    if v.parent_id and v.operation == 'INCREMENTAL_EXTRACT':
        parent_summary = await db.get(AttachmentVersionSummary, v.parent_id)
        if parent_summary and parent_summary.summary:
            carried = parent_summary.summary.get('cumulative_duration_seconds') or 0
        else:
            parent = await db.get(AttachmentVersion, v.parent_id)
            carried = ((parent.snapshot or {}).get('cumulative_duration_seconds') or 0) if parent else 0
    v.completed_at = datetime.utcnow()
    v.snapshot = {'contract_version': 'attachment-result-v1', 'version_id': str(v.id), 'number': v.number,
        'metadata': public_metadata(a), 'fields': rows, 'stats': field_stats(rows),
        'configuration': safe_config(json_value(profile)), 'configuration_hash': digest(safe_config(profile)),
        'rule_hashes': {r['field_key']: r['rule_hash'] for r in rows},
        'model_signature': profile.get('attachment_model_signature'), 'content_revision_id': str(run.content_revision_id),
        'file_revision_id': str(v.file_revision_id), 'invalidation_watermark': head.invalidation_watermark,
        'reviews': json_value([{c.name: getattr(r, c.name) for c in r.__table__.columns} for r in reviews]),
        'duration_seconds': run.duration_seconds,
        'cumulative_duration_seconds': (run.duration_seconds or 0) + carried}
    db.add(AttachmentVersionSummary(version_id=v.id, summary=summary_from_snapshot(v.snapshot)))
    a.current_version_id, a.cas_version, a.status = v.id, a.cas_version + 1, 'READY'
    # 管理附件只在同一事务中更新两层指针，避免补提 3 项的裸运行成为 8 项业务结果。
    head.current_parse_run_id, head.cas_version = run.parse_run_id, head.cas_version + 1
    document.parse_status = 'COMPLETED'
    await db.flush()
    return True
