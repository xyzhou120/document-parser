"""可选 P16 事务测试；仅在隔离的 G1 PostgreSQL 数据库上运行。"""

import os
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.models.assistant import QAMessage
from app.models.content import ContentRevision, DocumentProcessingHead, IndexChunk, IndexGeneration
from app.models.database import Document, DocumentArtifact
from app.models.execution import ExecutionOutbox
from app.services.assistant.qa_service import QARepository


@pytest_asyncio.fixture
async def sessions():
    name = os.environ.get("P05_TEST_DATABASE_NAME", "")
    if not name:
        pytest.skip("set P05_TEST_DATABASE_NAME to the isolated G1 database")
    if not name.startswith("document_parser_v2_g1_"):
        raise ValueError("P16 database must use the isolated G1 prefix")
    engine = create_async_engine(make_url(settings.database.url).set(database=name), echo=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_sql_qa_message_request_id_outbox_and_lease_fence(sessions):
    document_id, artifact_id, revision_id, generation_id = (uuid4() for _ in range(4))
    async with sessions() as db, db.begin():
        db.add(Document(document_id=document_id, file_name="p16.pdf", original_name="p16",
                        file_path="test/p16", file_size=1, file_hash="a" * 64, mime_type="application/pdf"))
        await db.flush()
        db.add(DocumentArtifact(artifact_id=artifact_id, document_id=document_id, file_hash="a" * 64,
                                preprocess_profile_version="test", status="READY", pages=[]))
        await db.flush()
        db.add(ContentRevision(revision_id=revision_id, document_id=document_id, artifact_id=artifact_id,
                               file_hash="a" * 64, content_digest="b" * 64, adapter_version="test",
                               source_kind="test", change_kind="initial", coverage_digest="c" * 64))
        await db.flush()
        db.add(IndexGeneration(generation_id=generation_id, revision_id=revision_id, config_hash="d" * 64,
                               snapshot_hash="e" * 64, build_status="succeeded", coverage_status="full",
                               serve_status="eligible", serve_version=1,
                               indexed_block_manifest={"document_id": str(document_id), "chunks": []}))
        await db.flush()
        db.add(DocumentProcessingHead(document_id=document_id, active_content_revision_id=revision_id,
                                      cas_version=0, invalidation_watermark=0))
        db.add(IndexChunk(generation_id=generation_id, chunk_id="b1", text="压力 1.2 MPa",
                          block_ref={"block_id": "b1", "page_number": 1}, table_ref=None))
        await db.flush()
        head = await db.get(DocumentProcessingHead, document_id, with_for_update=True)
        head.active_index_generation_id = generation_id
    repository = QARepository(sessions)
    session = await repository.create_session(document_id=document_id, revision_id=revision_id,
                                              generation_id=generation_id, request_id="p16-session")
    first, created = await repository.create_message(
        session_id=session["session_id"], client_message_id="p16-message", question="压力",
        mode="answer", revision_id=revision_id, generation_id=generation_id,
        scope_snapshot={"document_id": str(document_id), "revision_id": str(revision_id),
                        "generation_id": str(generation_id), "serve_version": 1,
                        "invalidation_watermark": 0})
    replay, duplicate = await repository.create_message(
        session_id=session["session_id"], client_message_id="p16-message", question="压力",
        mode="answer", revision_id=revision_id, generation_id=generation_id,
        scope_snapshot=first["scope_snapshot"])
    assert created and not duplicate and replay["message_id"] == first["message_id"]
    claimed = await repository.claim(first["message_id"])
    assert claimed and claimed["generation_fence"] == 1
    assert await repository.claim(first["message_id"]) is None
    async with sessions() as db:
        outbox = (await db.execute(select(ExecutionOutbox).where(
            ExecutionOutbox.kind == "assistant.message.execute",
            ExecutionOutbox.aggregate_id == first["message_id"]))).scalars().all()
        assert len(outbox) == 1
        row = await db.get(QAMessage, first["message_id"])
        assert row.execution_state == "running"
