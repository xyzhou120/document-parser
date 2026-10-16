"""显式临时数据库验收；永不连接业务库，schema 每项隔离。"""
import asyncio
import os
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from migrations.metadata import load_metadata
from app.models.attachments import APPEND_ONLY_DDL, Attachment, AttachmentFileRevision, AttachmentImport, AttachmentImportRow, AttachmentVersion
from app.models.database import Document, DocumentArtifact, ParseRun
from app.models.execution import ParseFieldJob
from app.models.content import DocumentProcessingHead
from app.rules.spec import RuleSpec
from app.services.attachment_import_service import process_preview, process_registration, register_attachment, AttachmentConflict
from app.services.attachment_runtime import claim_download, download_attachment, lease_valid
from app.services.attachment_version_service import finalize_version, start_operation, VersionError
from tests.test_attachment_import import row, workbook


@pytest_asyncio.fixture
async def sessions(monkeypatch):
    url=os.getenv('ATTACHMENT_TEST_DATABASE_URL')
    if not url: pytest.skip('set ATTACHMENT_TEST_DATABASE_URL to an isolated attachment_import_test_* database')
    parsed=make_url(url)
    if not parsed.database.startswith('attachment_import_test_') or parsed.host not in {'localhost','127.0.0.1'}:
        raise ValueError('refusing non-isolated database')
    schema='test_'+uuid4().hex
    admin=create_async_engine(url,poolclass=NullPool)
    async with admin.begin() as c: await c.execute(text(f'CREATE SCHEMA {schema}'))
    engine=create_async_engine(url,poolclass=NullPool,connect_args={'server_settings':{'search_path':schema}})
    async with engine.begin() as c:
        await c.run_sync(load_metadata().create_all)
        for ddl in APPEND_ONLY_DDL: await c.execute(text(ddl))
    try: yield async_sessionmaker(engine,expire_on_commit=False)
    finally:
        await engine.dispose()
        async with admin.begin() as c: await c.execute(text(f'DROP SCHEMA {schema} CASCADE'))
        await admin.dispose()


async def make_import(sessions, values):
    async with sessions() as db,db.begin():
        b=AttachmentImport(source_system='test',file_name='test.xlsx',file_hash='a'*64,staging_file=workbook(values),actor='tester')
        db.add(b); await db.flush(); bid=b.id
    await process_preview(sessions,bid)
    return bid


@pytest.mark.asyncio
async def test_excel_http_upload_preview_and_pdf_download_without_key_or_source_policy(sessions, monkeypatch):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from uuid import UUID
    import fitz
    import httpx
    from fastapi import FastAPI
    from app.api import attachment_imports
    from app.config import settings
    from app.storage import get_db
    import app.services.attachment_runtime as runtime

    monkeypatch.setattr(settings.attachment_import, 'enabled', True)
    monkeypatch.setattr(settings.attachment_import, 'sources', {})
    monkeypatch.setattr(attachment_imports, 'wakeup', lambda: None)
    with fitz.open() as pdf:
        pdf.new_page()
        pdf_data = pdf.tobytes()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.path == '/sample.pdf?token=original'
            self.send_response(200)
            self.send_header('Content-Type', 'application/pdf')
            self.send_header('Content-Length', str(len(pdf_data)))
            self.end_headers()
            self.wfile.write(pdf_data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{server.server_port}/sample.pdf?token=original'
    data = workbook([row(attachment_link=url)])
    app = FastAPI()
    app.include_router(attachment_imports.router)

    async def dependency():
        async with sessions() as db:
            yield db

    app.dependency_overrides[get_db] = dependency
    objects = {}
    monkeypatch.setattr(runtime.minio_client, 'upload', lambda key, data: objects.setdefault(key, data))
    monkeypatch.setattr(runtime.minio_client, 'delete', lambda key: objects.pop(key, None))
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.post('/api/v1/imports/excel', files={'file': ('test.xlsx', data)}, data={'source_system': 'debug'})
            assert response.status_code == 202
            bid = UUID(response.json()['id'])
            async with sessions() as db:
                assert (await db.get(AttachmentImport, bid)).staging_file == data
            await process_preview(sessions, bid)
            detail = await client.get(f'/api/v1/imports/{bid}')
            assert detail.status_code == 200 and detail.json()['items'][0]['data']['attachment_link'] == url
            response = await client.post(f'/api/v1/imports/{bid}/confirm', json={})
            assert response.status_code == 202
        await process_registration(sessions, bid)
        async with sessions() as db:
            a = await db.scalar(select(Attachment))
            assert a.source_url == url
            aid = a.id
        await download_attachment(sessions, aid)
        async with sessions() as db:
            a = await db.get(Attachment, aid)
            assert a.download_state == 'SUCCEEDED' and a.current_revision_id, a.last_error
            assert (await db.get(Document, a.document_id)).mine_id == row()['mine_code']
            assert list(objects.values()) == [pdf_data]
    finally:
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.asyncio
async def test_preview_has_only_staging_and_confirm_reuses_with_observations(sessions):
    bid=await make_import(sessions,[row(),{**row(),'attachment_id':'2','attachment_name':'drawing.dwg'},row()])
    async with sessions() as db,db.begin():
        assert await db.scalar(select(func.count()).select_from(Attachment))==0
        assert await db.scalar(select(func.count()).select_from(Document))==0
        assert await db.scalar(select(func.count()).select_from(ParseRun))==0
        b=await db.get(AttachmentImport,bid); assert b.state=='PREVIEW_READY' and b.counts=={'VALID':2,'INVALID':1}
        await db.execute(update(AttachmentImportRow).where(AttachmentImportRow.import_id==bid,AttachmentImportRow.state=='VALID').values(state='SELECTED'))
        b.state='REGISTERING'
    await asyncio.gather(process_registration(sessions,bid),process_registration(sessions,bid))
    async with sessions() as db,db.begin():
        assert await db.scalar(select(func.count()).select_from(Attachment))==2
        unsupported=await db.scalar(select(Attachment).where(Attachment.format=='dwg'))
        assert unsupported.download_state=='TYPE_UNSUPPORTED' and unsupported.document_id is None
    second=await make_import(sessions,[{**row(),'attachment_name':'renamed.pdf','attachment_link':'https://files.example/new'}])
    async with sessions() as db,db.begin():
        await db.execute(update(AttachmentImportRow).where(AttachmentImportRow.import_id==second).values(state='SELECTED'))
        b=await db.get(AttachmentImport,second); b.state='REGISTERING'
    await process_registration(sessions,second)
    async with sessions() as db:
        assert await db.scalar(select(func.count()).select_from(Attachment))==2
        observation=await db.scalar(select(AttachmentImportRow).where(AttachmentImportRow.import_id==second))
        assert observation.state=='REUSED' and observation.observation['before']['attachment_name']=='测试.pdf'
        assert observation.observation['before']['attachment_link'] == row()['attachment_link']
        assert observation.observation['after']['attachment_link'] == 'https://files.example/new'


@pytest.mark.asyncio
async def test_concurrent_identity_and_scope_conflicts(sessions):
    from app.services.attachment_excel import validate_row
    normalized,_=validate_row(row())
    async def register():
        async with sessions() as db,db.begin():
            a,_,_=await register_attachment(db,source_system='test',row=normalized,checklist_type=None)
            return a.id
    ids=await asyncio.gather(*[register() for _ in range(4)])
    assert len(set(ids))==1
    async with sessions() as db,db.begin():
        with pytest.raises(AttachmentConflict):
            await register_attachment(db,source_system='test',row={**normalized,'mine_code':'another'},checklist_type=None)


@pytest.mark.asyncio
async def test_download_fence_cancel_takeover_and_shared_bytes(sessions,monkeypatch):
    from app.services.attachment_excel import validate_row
    import app.services.attachment_runtime as runtime
    from app.config import settings
    monkeypatch.setattr(settings.attachment_import,'download_concurrency',3)
    ids=[]
    async with sessions() as db,db.begin():
        for source in ('1','2'):
            normalized,_=validate_row({**row(),'attachment_id':source})
            a,_,_=await register_attachment(db,source_system='test',row=normalized,checklist_type=None); ids.append(a.id)
    async with sessions() as db,db.begin():
        old=await claim_download(db,ids[0],'old')
        a=await db.get(Attachment,ids[0]); a.lease_until=datetime.utcnow()-timedelta(seconds=1)
    async with sessions() as db,db.begin():
        new=await claim_download(db,ids[0],'new'); a=await db.get(Attachment,ids[0])
        assert new['fence']>old['fence'] and not lease_valid(a,old) and lease_valid(a,new)
        a.download_state='CANCELLED'; a.download_fence+=1
        assert not lease_valid(a,new)
        a.download_state='QUEUED'; a.download_attempts=0
    store={}
    monkeypatch.setattr(runtime,'fetch_pdf',lambda *a,**k:(b'%PDF-1.7 test',dict(sha256='f'*64,size=13,mime_type='application/pdf',page_count=1,source_digest='e'*64)))
    monkeypatch.setattr(runtime.minio_client,'upload',lambda key,data:store.setdefault(key,data))
    monkeypatch.setattr(runtime.minio_client,'delete',lambda key:store.pop(key,None))
    for id in ids: await download_attachment(sessions,id)
    async with sessions() as db:
        documents=list((await db.scalars(select(Document))).all())
        assert len(documents)==2 and len({d.file_path for d in documents})==1 and len(store)==1
    # 同 ID 新内容：保留异常修订，既有 Document 和当前文件不变。
    monkeypatch.setattr(runtime,'fetch_pdf',lambda *a,**k:(b'%PDF-different',dict(sha256='d'*64,size=14,mime_type='application/pdf',page_count=1,source_digest='e'*64)))
    async with sessions() as db,db.begin():
        a=await db.get(Attachment,ids[0]); previous=a.current_revision_id; a.download_state='QUEUED'; a.download_attempts=0
    await download_attachment(sessions,ids[0])
    async with sessions() as db:
        a=await db.get(Attachment,ids[0]); assert a.status=='ID_CONTENT_CONFLICT' and a.current_revision_id==previous
        assert await db.scalar(select(func.count()).select_from(AttachmentFileRevision).where(AttachmentFileRevision.attachment_id==a.id))==2


async def seed_version_scope(sessions,monkeypatch):
    from app.rules.repository import rule_repository
    from app.config import settings
    from app.services.content_publish_service import content_publish_service
    from app.services.attachment_version_service import ensure_manual_attachment
    rules=[RuleSpec(field_key=f'f{i}',field_name_cn=f'字段{i}') for i in range(5)]
    async def get_rules(*a,**k): return rules
    monkeypatch.setattr(rule_repository,'get_rules_for_document',get_rules)
    monkeypatch.setattr(settings.app,'reliable_enabled',True)
    async with sessions() as db,db.begin():
        d=Document(document_id=uuid4(),file_name='v.pdf',original_name='v.pdf',file_path='test/v',file_size=10,file_hash='b'*64,mime_type='application/pdf',checklist_type='test')
        db.add(d); await db.flush()
        a=await ensure_manual_attachment(db,d)
        artifact=DocumentArtifact(document_id=d.document_id,file_hash=d.file_hash,preprocess_profile_version='test',status='READY',
            pages=[{'page_number':1,'text':'1','native_text':'1','text_blocks':[{'text':'1','source':'native','start_offset':0,'end_offset':1}]}],
            quality={'page_count':1,'coverage_manifest':{'expected_page_count':1,'pages':[{'page_number':1,'scope_complete':True,'regions':[{'region_id':'page','state':'processed'}]}]}})
        db.add(artifact); await db.flush(); await content_publish_service.publish_artifact(db,artifact,expected_cas_version=0,suppress_successor=True)
        aid=a.id
    return aid,rules


async def finish(sessions,vid,failed=False,review=False):
    async with sessions() as db,db.begin():
        v=await db.get(AttachmentVersion,vid); run=await db.get(ParseRun,v.run_id)
        jobs=(await db.scalars(select(ParseFieldJob).where(ParseFieldJob.run_id==run.parse_run_id))).all()
        for j in jobs: j.execution_state='done'; j.semantic_state='not_found'
        if failed: jobs[0].execution_state='failed_terminal'; jobs[0].semantic_state='incomplete'; run.status='FAILED'
        elif review: jobs[0].semantic_state='review_required'; run.status='COMPLETED_WITH_REVIEW'
        else: run.status='COMPLETED'
        await db.flush(); await finalize_version(db,run)


@pytest.mark.asyncio
async def test_done_but_incomplete_real_aggregation_retains_current_version(sessions, monkeypatch):
    from app.services.reliable_extraction_service import reliable_extraction_service

    aid, _ = await seed_version_scope(sessions, monkeypatch)
    async with sessions() as db, db.begin():
        v = await start_operation(db, aid, operation='INITIAL_EXTRACT', request_id='valid', actor='test', reason='baseline')
        v1 = v.id
    await finish(sessions, v1)
    async with sessions() as db, db.begin():
        v = await start_operation(db, aid, operation='FULL_REEXTRACT', request_id='incomplete', actor='test', reason='boundary')
        v2, run_id = v.id, v.run_id
        jobs = list((await db.scalars(select(ParseFieldJob).where(ParseFieldJob.run_id == run_id))).all())
        for job in jobs:
            job.execution_state, job.semantic_state = 'done', 'not_found'
        # 故障注入/旧数据：执行已结束，但检索或证据范围没有闭合。
        jobs[0].semantic_state = 'incomplete'

    async def unexpected_execution(*args, **kwargs):
        raise AssertionError('terminal jobs must not execute again')

    result = await reliable_extraction_service.process_run(sessions, run_id,
        executor=unexpected_execution, owner='incomplete-regression')
    assert result['status'] == 'FAILED' and result['quantums_executed'] == 0
    async with sessions() as db:
        a = await db.get(Attachment, aid)
        assert a.current_version_id == v1
        version = await db.get(AttachmentVersion, v2)
        assert version.state == 'FAILED' and version.snapshot is None
        assert (await db.get(ParseRun, run_id)).status == 'FAILED'
        assert (await db.get(DocumentProcessingHead, a.document_id)).current_parse_run_id == (await db.get(AttachmentVersion, v1)).run_id


@pytest.mark.asyncio
async def test_five_plus_three_retry_current_pointer_and_immutable_history(sessions,monkeypatch):
    aid,rules=await seed_version_scope(sessions,monkeypatch)
    async with sessions() as db,db.begin():
        v=await start_operation(db,aid,operation='INITIAL_EXTRACT',request_id='first',actor='test',reason='first'); v1=v.id
    await finish(sessions,v1,review=True)
    async with sessions() as db:
        a=await db.get(Attachment,aid); assert a.current_version_id==v1
        snap1=(await db.get(AttachmentVersion,v1)).snapshot
        assert snap1['stats']['targets']==5 and snap1['stats']['review_required']==1
    rules.extend(RuleSpec(field_key=f'f{i}',field_name_cn=f'字段{i}') for i in range(5,8))
    async with sessions() as db,db.begin():
        v=await start_operation(db,aid,operation='INCREMENTAL_EXTRACT',request_id='supplement',actor='test',reason='new targets'); v2=v.id
        assert v.number==2 and len(v.inherited_fields)==5
        assert (await db.get(Attachment,aid)).current_version_id==v1
    await finish(sessions,v2,failed=True)
    async with sessions() as db,db.begin():
        assert (await db.get(Attachment,aid)).current_version_id==v1
        same=await start_operation(db,aid,operation='INCREMENTAL_EXTRACT',request_id='supplement',actor='test',reason='retry')
        assert same.id==v2 and same.number==2
        same.state='RUNNING'
    await finish(sessions,v2)
    async with sessions() as db:
        a=await db.get(Attachment,aid); current=await db.get(AttachmentVersion,a.current_version_id)
        assert current.id==v2 and current.snapshot['stats']['targets']==8
        assert (await db.get(AttachmentVersion,v1)).snapshot==snap1
        head=await db.get(DocumentProcessingHead,a.document_id); assert head.current_parse_run_id==current.run_id
    async with sessions() as db:
        with pytest.raises(DBAPIError):
            await db.execute(update(AttachmentVersion).where(AttachmentVersion.id==v1).values(snapshot={'overwrite':True})); await db.commit()
        await db.rollback()
    async with sessions() as db,db.begin():
        v=await start_operation(db,aid,operation='FULL_REEXTRACT',request_id='full',actor='test',reason='model changed')
        assert v.number==3 and not v.inherited_fields
        assert (await db.get(Attachment,aid)).current_version_id==v2
        with pytest.raises(VersionError,match='ACTIVE'):
            await start_operation(db,aid,operation='FULL_REEXTRACT',request_id='another',actor='test',reason='concurrent')


@pytest.mark.asyncio
async def test_invalid_url_is_row_error_and_original_links_are_preserved(sessions):
    values = [row(), {**row(), 'attachment_id': '2', 'attachment_link': 'https://files.example:bad/a?secret=hidden'},
              {**row(), 'attachment_id': '3', 'attachment_link': 'HTTPS://files.example/a?signature=hidden'}]
    bid = await make_import(sessions, values)
    async with sessions() as db:
        batch = await db.get(AttachmentImport, bid)
        assert batch.state == 'PREVIEW_READY' and batch.staging_file is None
        assert batch.counts == {'VALID': 2, 'INVALID': 1}
        for item in (await db.scalars(select(AttachmentImportRow).where(AttachmentImportRow.import_id == bid)
                .order_by(AttachmentImportRow.row_number))).all():
            expected = values[item.row_number - 2]['attachment_link']
            assert item.raw['attachment_link'] == expected
            assert item.normalized['attachment_link'] == expected
            assert item.source_url == expected


async def add_fact_fields(db, version):
    from app.models.database import ExtractedFieldModel
    run = await db.get(ParseRun, version.run_id)
    for key, value in [('f0', '10'), ('f1', '20')]:
        assertions = {k: {'status': 'supported', 'basis': 'literal', 'evidence_ids': ['ev']} for k in ('value', 'unit', 'value_kind', 'source_role')}
        assertions['table_relation'] = {'status': 'not_applicable', 'basis': 'source.non_table', 'evidence_ids': []}
        item = {'value': value, 'unit': 'm', 'value_kind': 'measured', 'source_role': 'report',
                'dimensions': {}, 'content_revision': str(run.content_revision_id), 'source_validity': 'valid',
                'assertions': assertions, 'evidence_ids': ['ev'], 'page_num': 1, 'quote': value}
        db.add(ExtractedFieldModel(parse_run_id=run.parse_run_id, document_id=run.document_id,
            field_key=key, field_name=key, field_value=value, extraction_method='agent_extract',
            validation_status='completed', instances=[item], unit='m',
            extraction_detail={'contract_version': 'reliable-extraction-v2', 'semantic_state': 'completed',
                'coverage_complete': True, 'instances': [item], 'evidence_registry': {'ev': {'page_number': 1, 'quote': value}}}))


@pytest.mark.asyncio
async def test_current_review_overlay_frozen_history_and_selective_invalidation(sessions, monkeypatch):
    from copy import deepcopy
    from app.models.reviews import FieldReviewRevision
    from app.models.content import ContentInvalidation
    from app.services.attachment_read_service import snapshot_fact_view, attachment_page
    aid, rules = await seed_version_scope(sessions, monkeypatch)
    async with sessions() as db, db.begin():
        first = await start_operation(db, aid, operation='INITIAL_EXTRACT', request_id='facts', actor='test', reason='facts')
        v1 = first.id
        await add_fact_fields(db, first)
    await finish(sessions, v1)
    async with sessions() as db, db.begin():
        first = await db.get(AttachmentVersion, v1)
        frozen = deepcopy(first.snapshot)
        automatic = await snapshot_fact_view(db, first, view='automatic', overlay=True)
        assert {f.field_key for f in automatic.facts} == {'f0', 'f1'}
        assert all(f.available_for_downstream for f in automatic.facts)
        a = await db.get(Attachment, aid)
        db.add(FieldReviewRevision(parse_run_id=first.run_id, document_id=a.document_id, field_key='f0',
            revision=1, previous_revision=0, action='CORRECT', reviewer='tester', reason='manual correction',
            file_hash='b'*64, before_snapshot={}, facts=[{'value': '11', 'unit': 'm', 'source_role': 'report',
            'value_kind': 'measured', 'page_num': 1, 'quote': '11'}]))
    async with sessions() as db:
        first = await db.get(AttachmentVersion, v1)
        reviewed = await snapshot_fact_view(db, first, view='reviewed', overlay=True)
        assert [f.value for f in reviewed.facts] == ['11']
        assert not (await snapshot_fact_view(db, first, view='reviewed')).facts
        assert len((await snapshot_fact_view(db, first, view='available', overlay=True)).facts) == 2
    rules.extend([RuleSpec(field_key='f5', field_name_cn='新增')])
    async with sessions() as db, db.begin():
        second = await start_operation(db, aid, operation='INCREMENTAL_EXTRACT', request_id='more', actor='test', reason='new field')
        v2 = second.id
    await finish(sessions, v2)
    async with sessions() as db, db.begin():
        first, second = await db.get(AttachmentVersion, v1), await db.get(AttachmentVersion, v2)
        assert first.snapshot == frozen
        assert [f.value for f in (await snapshot_fact_view(db, second, view='reviewed')).facts] == ['11']
        a = await db.get(Attachment, aid)
        head = await db.get(DocumentProcessingHead, a.document_id)
        head.invalidation_watermark += 1
        db.add(ContentInvalidation(document_id=a.document_id, invalidation_kind='field',
            scope_selector={'field_keys': ['f0']}, reason='selective withdrawal'))
    async with sessions() as db:
        second = await db.get(AttachmentVersion, v2)
        current = await snapshot_fact_view(db, second, view='available', overlay=True)
        assert [f.field_key for f in current.facts] == ['f1']
        assert len((await snapshot_fact_view(db, second, view='available')).facts) == 2
        page = await attachment_page(db, mine='', checklist='test', name='v.pdf', period_type='unknown', current_state='COMPLETED')
        assert page['total'] == 1 and page['items'][0]['current']['id'] == v2
        assert page['items'][0]['current_validity'] == 'RECHECK_REQUIRED'


@pytest.mark.asyncio
async def test_full_failure_cancel_success_and_concurrent_control_finalize(sessions, monkeypatch):
    from app.services.attachment_control_service import control_attachment
    from app.services.reliable_extraction_service import reliable_extraction_service
    aid, _ = await seed_version_scope(sessions, monkeypatch)
    async with sessions() as db, db.begin():
        v = await start_operation(db, aid, operation='INITIAL_EXTRACT', request_id='first', actor='test', reason='initial'); v1 = v.id
    await finish(sessions, v1)
    async with sessions() as db, db.begin():
        v = await start_operation(db, aid, operation='FULL_REEXTRACT', request_id='full', actor='test', reason='full'); v2 = v.id
    await finish(sessions, v2, failed=True)
    async with sessions() as db:
        assert (await db.get(Attachment, aid)).current_version_id == v1
    async with sessions() as db, db.begin():
        a0 = await db.get(Attachment, aid)
        await db.get(Document, a0.document_id, with_for_update=True)
        a = await db.get(Attachment, aid, with_for_update=True)
        await control_attachment(db, a, 'retry', actor='tester', reason='retry failed full')
        assert (await db.get(AttachmentVersion, v2)).number == 2
    await finish(sessions, v2)
    async with sessions() as db, db.begin():
        assert (await db.get(Attachment, aid)).current_version_id == v2
        v = await start_operation(db, aid, operation='FULL_REEXTRACT', request_id='cancel-full', actor='test', reason='full'); v3 = v.id; rid = v.run_id
    async def cancel():
        async with sessions() as db, db.begin():
            a0 = await db.get(Attachment, aid)
            await db.get(Document, a0.document_id, with_for_update=True)
            a = await db.get(Attachment, aid, with_for_update=True)
            await control_attachment(db, a, 'cancel', actor='tester', reason='cancel full')
    # 真实两会话并发执行聚合和控制，不能因锁顺序反转而死锁。
    async def executor(token, checkpoint):
        from app.services.execution import QuantumOutcome
        return QuantumOutcome('yielded', checkpoint)
    await asyncio.wait_for(asyncio.gather(cancel(), reliable_extraction_service.process_run(
        sessions, rid, owner='concurrency-test', executor=executor, max_quantums=1)), timeout=5)
    async with sessions() as db:
        assert (await db.get(Attachment, aid)).current_version_id == v2
        assert (await db.get(AttachmentVersion, v3)).state == 'CANCELLED'
        assert (await db.get(AttachmentVersion, v1)).snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['pause', 'cancel'])
async def test_content_bridge_respects_control_after_ocr_before_publication(sessions, monkeypatch, tmp_path, action):
    import hashlib
    from types import SimpleNamespace
    from app.config import settings
    from app.rules.repository import rule_repository
    from app.services.attachment_version_service import ensure_manual_attachment
    from app.services.attachment_control_service import control_attachment
    from app.services.content_preparation_service import ContentPreparationService
    from app.services.content_build_service import ContentBuildService
    from app.services.service_circuit import ServiceCircuit, MemoryCircuitStore
    from app.models.content import ContentBuildJob, ContentRevision
    source = b'isolated synthetic pdf'
    async def rules(*args, **kwargs): return [RuleSpec(field_key='f0', field_name_cn='字段')]
    monkeypatch.setattr(rule_repository, 'get_rules_for_document', rules)
    monkeypatch.setattr(settings.app, 'reliable_enabled', True)
    async with sessions() as db, db.begin():
        document = Document(file_name='bridge.pdf', original_name='bridge.pdf', file_path='test/bridge',
            file_size=len(source), file_hash=hashlib.sha256(source).hexdigest(), mime_type='application/pdf', checklist_type='test')
        db.add(document); await db.flush()
        a = await ensure_manual_attachment(db, document); aid = a.id; did = document.document_id
        version = await start_operation(db, aid, operation='INITIAL_EXTRACT', request_id='bridge', actor='test', reason='bridge')
        build_id, vid = version.build_id, version.id
        assert version.state == 'PREPARING_CONTENT'
    class Parser:
        def extract_pages(self, path): return [{'page_number': 1, 'text': 'native', 'native_text': 'native',
            'image_count': 0, 'width': 100, 'height': 200, 'tables': [], 'text_blocks': []}]
        def inspect_pages(self, path): return [SimpleNamespace(page_number=1, needs_ocr=False, is_scan=False)]
    service = ContentPreparationService(parser=Parser(), builder=ContentBuildService(circuit=ServiceCircuit(MemoryCircuitStore())), cache_dir=tmp_path)
    reached = asyncio.Event(); release = asyncio.Event()
    original = service.builder.process
    async def gated(*args, **kwargs):
        result = await original(*args, **kwargs)
        reached.set()
        await release.wait()
        return result
    monkeypatch.setattr(service.builder, 'process', gated)
    task = asyncio.create_task(service.run_build(sessions, build_id, source_resolver=lambda _: source))
    try:
        await asyncio.wait_for(reached.wait(), 5)
        async with sessions() as db, db.begin():
            await db.get(Document, did, with_for_update=True)
            a = await db.get(Attachment, aid, with_for_update=True)
            await control_attachment(db, a, action, actor='test', reason='publication race')
        release.set()
        result = await asyncio.wait_for(task, 5)
        assert result['status'] == 'cancelled_or_paused'
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    async with sessions() as db:
        assert (await db.get(AttachmentVersion, vid)).state == {'pause': 'PAUSED', 'cancel': 'CANCELLED'}[action]
        assert await db.scalar(select(func.count()).select_from(ContentRevision).where(ContentRevision.document_id == did)) == 0
        assert await db.scalar(select(func.count()).select_from(ParseRun).where(ParseRun.document_id == did)) == 0


@pytest.mark.asyncio
async def test_attempt_cleanup_preserves_shared_references_live_leases_and_recent_objects(sessions, monkeypatch):
    from types import SimpleNamespace
    import app.services.attachment_runtime as runtime
    from app.services.attachment_version_service import ensure_manual_attachment
    async with sessions() as db, db.begin():
        doc = Document(file_name='clean.pdf', original_name='clean.pdf', file_path='temp', file_size=1,
            file_hash='c'*64, mime_type='application/pdf')
        db.add(doc); await db.flush()
        a = await ensure_manual_attachment(db, doc); aid = a.id
        key = f'attachment-attempts/{aid}/committed.pdf'
        db.add(AttachmentFileRevision(attachment_id=aid, sha256='d'*64, size=1, mime_type='application/pdf',
            object_key=key, source_digest='x', state='ID_CONTENT_CONFLICT', metadata_snapshot={}))
    old = datetime.utcnow() - timedelta(days=2)
    objects = [SimpleNamespace(object_name=key, last_modified=old),
        SimpleNamespace(object_name=f'attachment-attempts/{aid}/orphan.pdf', last_modified=old),
        SimpleNamespace(object_name=f'attachment-attempts/{aid}/recent.pdf', last_modified=datetime.utcnow())]
    deleted = []
    class Client:
        def list_objects(self, *args, **kwargs): return iter(objects)
    monkeypatch.setattr(runtime.minio_client, '_client', Client())
    monkeypatch.setattr(runtime.minio_client, 'delete', lambda key: deleted.append(key))
    result = await runtime.cleanup_attempt_objects(sessions)
    assert result['removed'] == 1 and deleted == [objects[1].object_name]
    async with sessions() as db, db.begin():
        a = await db.get(Attachment, aid); a.lease_until = datetime.utcnow() + timedelta(minutes=1)
    deleted.clear()
    assert (await runtime.cleanup_attempt_objects(sessions))['removed'] == 0


@pytest.mark.asyncio
async def test_http_attachment_reads_export_snapshot_and_inherited_review_source(sessions, monkeypatch):
    import httpx
    from fastapi import FastAPI
    from app.api.attachments import router
    from app.api.parse import router as parse_router
    from app.storage import get_db
    aid, rules = await seed_version_scope(sessions, monkeypatch)
    async with sessions() as db, db.begin():
        first = await start_operation(db, aid, operation='INITIAL_EXTRACT', request_id='api1', actor='test', reason='api'); v1 = first.id
        await add_fact_fields(db, first)
    await finish(sessions, v1)
    rules.append(RuleSpec(field_key='f5', field_name_cn='新增'))
    async with sessions() as db, db.begin():
        second = await start_operation(db, aid, operation='INCREMENTAL_EXTRACT', request_id='api2', actor='test', reason='api'); v2 = second.id; rid = second.run_id
    await finish(sessions, v2)
    app = FastAPI(); app.include_router(router); app.include_router(parse_router)
    async def dependency():
        async with sessions() as db:
            yield db
    app.dependency_overrides[get_db] = dependency
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.get('/api/v1/attachments', params={'checklist': 'test', 'current_state': 'COMPLETED'})
        assert response.status_code == 200 and response.json()['total'] == 1
        response = await client.get(f'/api/v1/attachments/{aid}/versions/{v1}')
        assert response.status_code == 200 and response.json()['snapshot']['stats']['targets'] == 5
        response = await client.get('/api/v1/attachments/export', params={'checklist': 'test'})
        assert response.status_code == 200 and 'v.pdf' in response.text and 'source_url' not in response.text
        response = await client.get(f'/api/v1/parse/runs/{rid}')
        assert response.status_code == 200
        fields = response.json()['fields']
        assert {f['source_parse_run_id'] for f in fields} == {str((await client.get(f'/api/v1/attachments/{aid}/versions/{v1}')).json()['run_id'])}
        response = await client.get(f'/api/v1/attachments/{aid}/current/facts')
        assert response.status_code == 200 and response.json()['schema_version'] == 'v2'
