"""共享结果验收只使用显式隔离 PostgreSQL 库和模拟模型。"""
import asyncio
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from app.config import settings
from app.models.attachments import Attachment, AttachmentVersion
from app.models.content import ContentRevision
from app.models.database import ExtractedFieldModel, ParseRun
from app.models.execution import ParseCandidate, ParseFieldJob
from app.models.shared_execution import SharedExecution, SharedExecutionSubscriber
from app.services.attachment_control_service import control_attachment
from app.services.reliable_extraction_service import ReliableExtractionService
from app.services.shared_execution_service import materialize_execution
from tests.test_field_scheduler_postgres import sessions as pg_sessions
from tests.test_reliable_field_executor import make_run
from tests.test_page_batch_postgres import SharedProvider, executor

sessions = pg_sessions


@pytest.mark.asyncio
async def test_shared_election_materialization_and_cancellation_are_independent(sessions, monkeypatch):
    monkeypatch.setattr(settings.agent, 'shared_execution_enabled', True)
    unique = f'报告正文 瓦斯压力 1.23MPa 实测 {uuid4()}'
    one, two = await asyncio.gather(*[
        make_run(sessions, 2, extraction_strategy='page_batch_v1', text_override=unique) for _ in range(2)])
    async with sessions() as db:
        runs = [await db.get(ParseRun, identity) for identity in [one, two]]
        assert all(r.status == 'WAITING_REUSE' for r in runs)
        identities = {r.extraction_profile['shared_execution_id'] for r in runs}
        assert len(identities) == 1
        execution = await db.get(SharedExecution, UUID(identities.pop()))
        owner_id, execution_id = execution.source_run_id, execution.shared_execution_id
        assert owner_id not in {one, two}
        revisions = {r.content_revision_id for r in runs}
        assert len(revisions) == 2
        for revision in revisions:
            assert (await db.get(ContentRevision, revision)).document_id in {r.document_id for r in runs}
    # Cancel the business attachment that originally created the owner.
    async with sessions() as db, db.begin():
        run = await db.get(ParseRun, one)
        attachment = await db.scalar(select(Attachment).where(Attachment.document_id == run.document_id))
        await control_attachment(db, attachment, 'cancel', actor='test', reason='independent cancel')
        owner = await db.get(ParseRun, owner_id)
        assert owner.status == 'PLANNED'
    provider = SharedProvider()
    instance = executor(sessions, provider)
    result = await ReliableExtractionService().process_run(sessions, owner_id, executor=instance,
        writer=instance.write_result, before_publish=instance.before_publish,
        owner='shared-test', max_quantums=10, lease_seconds=300)
    assert result['status'] == 'COMPLETED'
    first = await materialize_execution(sessions, execution_id)
    await materialize_execution(sessions, execution_id)
    assert first['completed'] == 1
    assert provider.calls == 1
    async with sessions() as db:
        target = await db.get(ParseRun, two)
        source = await db.get(ParseRun, owner_id)
        assert target.status == 'COMPLETED'
        assert (await db.get(ParseRun, one)).status == 'CANCELLED'
        version = await db.get(AttachmentVersion, UUID(target.extraction_profile['attachment_version_id']))
        assert version.snapshot and version.snapshot['content_revision_id'] == str(target.content_revision_id)
        fields = list((await db.scalars(select(ExtractedFieldModel).where(
            ExtractedFieldModel.parse_run_id == two))).all())
        assert len(fields) == 2
        for field in fields:
            assert field.document_id == target.document_id
            assert field.extraction_detail['shared_provenance']['reused_from_run_id'] == str(owner_id)
            registry = field.extraction_detail['evidence_registry']
            assert registry and all(item['content_revision'] == str(target.content_revision_id)
                                   for item in registry.values())
            assert all(item['content_revision'] != str(source.content_revision_id) for item in registry.values())
        assert await db.scalar(select(func.count()).select_from(ParseCandidate).join(ParseFieldJob).where(
            ParseFieldJob.run_id == two)) == 2


@pytest.mark.asyncio
async def test_failed_shared_owner_restores_target_own_execution(sessions, monkeypatch):
    monkeypatch.setattr(settings.agent, 'shared_execution_enabled', True)
    target_id = await make_run(sessions, 3, extraction_strategy='page_batch_v1', text_override=f'报告正文 另一份测试 {uuid4()}')
    async with sessions() as db, db.begin():
        target = await db.get(ParseRun, target_id)
        execution_id = UUID(target.extraction_profile['shared_execution_id'])
        execution = await db.get(SharedExecution, execution_id)
        source = await db.get(ParseRun, execution.source_run_id)
        source.status, execution.state = 'FAILED', 'FAILED'
    result = await materialize_execution(sessions, execution_id)
    assert result['states'] == ['FALLBACK']
    async with sessions() as db:
        target = await db.get(ParseRun, target_id)
        assert target.status == 'PLANNED'
        jobs = (await db.scalars(select(ParseFieldJob).where(ParseFieldJob.run_id == target_id))).all()
        assert all(job.execution_state == 'queued' for job in jobs)


@pytest.mark.asyncio
async def test_target_invalidation_does_not_block_other_subscribers(sessions, monkeypatch):
    from app.models.content import ContentInvalidation, DocumentProcessingHead
    monkeypatch.setattr(settings.agent, 'shared_execution_enabled', True)
    unique = f'报告正文 瓦斯压力 1.23MPa 实测 {uuid4()}'
    one, two = [await make_run(sessions, 2, extraction_strategy='page_batch_v1',
                             text_override=unique) for _ in range(2)]
    async with sessions() as db, db.begin():
        target = await db.get(ParseRun, one)
        execution_id = UUID(target.extraction_profile['shared_execution_id'])
        execution = await db.get(SharedExecution, execution_id)
        owner_id = execution.source_run_id
    provider = SharedProvider()
    instance = executor(sessions, provider)
    result = await ReliableExtractionService().process_run(sessions, owner_id, executor=instance,
        writer=instance.write_result, before_publish=instance.before_publish,
        owner='invalidated-source-test', max_quantums=10, lease_seconds=300)
    assert result['status'] == 'COMPLETED'
    async with sessions() as db, db.begin():
        target = await db.get(ParseRun, one)
        db.add(ContentInvalidation(document_id=target.document_id, invalidation_kind='document',
                                  scope_selector={}, reason='source-only invalidation'))
        head = await db.get(DocumentProcessingHead, target.document_id, with_for_update=True)
        head.invalidation_watermark += 1
    result = await materialize_execution(sessions, execution_id)
    assert result['states'].count('SUPERSEDED') == 1
    assert result['completed'] == 1
    async with sessions() as db:
        assert (await db.get(ParseRun, one)).status == 'SUPERSEDED'
        assert (await db.get(ParseRun, two)).status == 'COMPLETED'
