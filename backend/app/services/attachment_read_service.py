"""业务读取只通过已完成快照；动态失效另列，不改写历史。"""
from copy import deepcopy
from uuid import UUID

from sqlalchemy import func, inspect, select
from sqlalchemy.orm import aliased, defer

from app.models.attachments import Attachment, AttachmentImportRow, AttachmentVersion, AttachmentVersionSummary
from app.models.content import DocumentProcessingHead
from app.models.database import ParseRun
from app.services.attachment_import_service import public_metadata

SUMMARY_KEYS = ('stats', 'duration_seconds', 'cumulative_duration_seconds', 'content_revision_id', 'invalidation_watermark')


def summary_from_snapshot(snapshot):
    if not snapshot:
        return None
    return {key: snapshot.get(key) for key in SUMMARY_KEYS}


def summary_payload(summary_row, version):
    """优先用摘要行。版本行的大 snapshot 被延迟加载时不能触发读取。"""
    if summary_row is not None:
        return summary_row.summary or {}
    if version is None or 'snapshot' in inspect(version).unloaded:
        return {}
    return summary_from_snapshot(version.snapshot) or {}


def version_dict(v, *, summary=None, include_snapshot=False):
    if v is None:
        return None
    data = summary_payload(summary, v)
    result = {k: getattr(v, k) for k in ('id', 'number', 'operation', 'request_id', 'parent_id', 'run_id', 'build_id',
        'state', 'actor', 'reason', 'created_at', 'completed_at', 'last_error')}
    result['stats'] = data.get('stats')
    result['duration_seconds'] = data.get('duration_seconds')
    if include_snapshot:
        result['snapshot'] = v.snapshot
    return result


def _watermark(value):
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def attachment_dict(a, current=None, latest=None, head=None, current_summary=None, latest_summary=None):
    data = summary_payload(current_summary, current)
    validity = 'NO_CURRENT_RESULT'
    if data:
        revision = data.get('content_revision_id')
        validity = ('CONTENT_CHANGED' if not head or str(head.active_content_revision_id) != (None if revision is None else str(revision))
            else 'RECHECK_REQUIRED' if head.invalidation_watermark != _watermark(data.get('invalidation_watermark')) else 'VALID')
    return {**public_metadata(a), **{key: getattr(a, key) for key in ('id', 'document_id', 'status', 'download_state',
        'download_attempts', 'last_error', 'created_at', 'updated_at')},
        'current': version_dict(current, summary=current_summary), 'latest': version_dict(latest, summary=latest_summary),
        'current_validity': validity}


async def attachment_page(db, *, page=1, page_size=20, mine=None, checklist=None, file_name_id=None,
                          period_type=None, period_key=None, name=None, status=None, download_state=None,
                          import_id=None, latest_state=None, format=None, current_state=None):
    current, latest = aliased(AttachmentVersion), aliased(AttachmentVersion)
    current_summary, latest_summary = aliased(AttachmentVersionSummary), aliased(AttachmentVersionSummary)
    last_number = select(func.max(AttachmentVersion.number)).where(AttachmentVersion.attachment_id == Attachment.id).correlate(Attachment).scalar_subquery()
    query = select(Attachment, current, latest, DocumentProcessingHead, current_summary, latest_summary)\
        .outerjoin(current, current.id == Attachment.current_version_id)\
        .outerjoin(current_summary, current_summary.version_id == current.id)\
        .outerjoin(latest, (latest.attachment_id == Attachment.id) & (latest.number == last_number))\
        .outerjoin(latest_summary, latest_summary.version_id == latest.id)\
        .outerjoin(DocumentProcessingHead, DocumentProcessingHead.document_id == Attachment.document_id)\
        .options(defer(current.snapshot), defer(latest.snapshot))
    if mine:
        query = query.where(Attachment.mine_code.contains(mine, autoescape=True) | Attachment.mine_name.contains(mine, autoescape=True))
    for col, value in ((Attachment.checklist_type, checklist), (Attachment.file_name_id, file_name_id),
                       (Attachment.period_type, period_type), (Attachment.period_key, period_key),
                       (Attachment.status, status), (Attachment.download_state, download_state),
                       (Attachment.format, format), (latest.state, latest_state)):
        if value:
            query = query.where(col == value)
    if current_state == 'COMPLETED':
        query = query.where(current.id.is_not(None))
    elif current_state == 'NONE':
        query = query.where(current.id.is_(None))
    if name:
        query = query.where(Attachment.attachment_name.contains(name, autoescape=True))
    if import_id:
        query = query.where(Attachment.id.in_(select(AttachmentImportRow.attachment_id).where(AttachmentImportRow.import_id == import_id)))
    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    rows = (await db.execute(query.order_by(Attachment.created_at.desc(), Attachment.id).limit(page_size).offset((page - 1) * page_size))).all()
    return {'total': total, 'items': [attachment_dict(*row) for row in rows]}


async def snapshot_fact_view(db, v, *, view='automatic', overlay=False):
    from app.schemas.parse import FactViewResponse
    from app.services.fact_policy_v2 import load_invalidations, matching_invalidations
    if not v.snapshot:
        return None
    a = await db.get(Attachment, v.attachment_id)
    facts, targets, missing = [], [], []
    invalidations = await load_invalidations(db, a.document_id) if overlay else []
    head = await db.get(DocumentProcessingHead, a.document_id) if overlay else None
    globally_invalid = bool(overlay and (not head or str(head.active_content_revision_id) != v.snapshot.get('content_revision_id')))
    runs = {}
    current_reviews = []
    for item in v.snapshot['fields']:
        source = 'reviewed' if view == 'reviewed' else 'automatic'
        selected = deepcopy(item['facts'].get(source, []))
        run_id = UUID(item['run_id'])
        if run_id not in runs:
            runs[run_id] = await db.get(ParseRun, run_id)
        run = runs[run_id]
        if overlay and run and item.get('field'):
            # 当前人工复核是独立追加层；历史快照仍使用定版时的复核水位。
            from app.models.reviews import FieldReviewRevision
            from app.services.fact_service import build_fact_view_with_invalidations
            revisions = (await db.scalars(select(FieldReviewRevision).where(
                FieldReviewRevision.parse_run_id == run_id, FieldReviewRevision.field_key == item['field_key'])
                .order_by(FieldReviewRevision.revision.desc()))).all()
            projected = await build_fact_view_with_invalidations(db, run, [item['field']], view='reviewed', revisions=revisions)
            current_reviews.extend(projected.reviews)
            if view == 'reviewed':
                selected = [fact.model_dump(mode='json') for fact in projected.facts]
        for fact in selected:
            if globally_invalid or (overlay and run and matching_invalidations(run, fact, invalidations)):
                fact['available_for_downstream'] = False
                fact['risk'] = [*fact.get('risk', []), {'code': 'CONTENT_INVALIDATED'}]
            if view != 'available' or fact.get('available_for_downstream'):
                facts.append(fact)
        targets.append({'target_key': item['field_key'], 'field_key': item['field_key'], 'status': item['semantic_state']})
        if not selected:
            missing.append({'field_key': item['field_key'], 'reason': item['semantic_state']})
    return FactViewResponse(parse_run_id=v.run_id, document_id=a.document_id, view=view, facts=facts,
        targets=targets, missing_fields=missing, reviews=current_reviews if overlay else v.snapshot.get('reviews', []),
        schema_version='v2', availability_policy='complete_scope_v2',
        contract_version='reliable-extraction-v2', warnings=['历史快照按定版时保存；当前失效信息另列。'] if not overlay else [])
