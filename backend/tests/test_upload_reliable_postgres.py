"""在授权的 G1 PostgreSQL 上测试新上传 -> v2 准备 -> 字段提取。

存储上传和消息代理发送使用模拟对象。准备、解析器和提供方使用明确的模拟输入；
文档、构建、发件箱、运行、任务、候选和事实的持久化全部使用真实 PostgreSQL。
"""

import hashlib
import io
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException, UploadFile
from sqlalchemy import func, select

from app.api.parse import upload_and_parse
from app.models.content import ContentBuildJob
from app.models.database import Document, ExtractedFieldModel, ParseRun
from app.models.execution import ExecutionOutbox, ParseFieldJob
from app.rules.spec import RuleSpec
from app.services.call_ledger import CallLedger
from app.services.content_build_service import ContentBuildService
from app.services.content_preparation_service import ContentPreparationService
from app.services.model_gateway import ModelGateway
from app.services.reliable_extraction_service import ReliableExtractionService
from app.services.reliable_field_executor import ReliableFieldExecutor, ReliableFieldExecutorConfig
from app.services.service_circuit import MemoryCircuitStore, ServiceCircuit
from tests.test_field_scheduler_postgres import sessions as pg_sessions
from tests.test_reliable_field_executor import FakeProvider

sessions = pg_sessions


class NativeOnePageParser:
    """小型显式页面夹具；不依赖真实 PDF 或 OCR 服务。"""

    def extract_pages(self, path):
        text = "报告正文 瓦斯压力 1.23MPa 实测"
        return [{"page_number": 1, "width": 600, "height": 800, "text": text,
                 "native_text": text, "image_count": 0, "tables": [],
                 "text_blocks": [{"text": text, "source": "native", "block_id": "upload-native",
                     "bbox": [0, 0, 500, 30], "coordinate_system": "PDF_POINT",
                     "start_offset": 0, "end_offset": len(text)}]}]

    def inspect_pages(self, path):
        return [SimpleNamespace(page_number=1, needs_ocr=False, is_scan=False)]


def rules():
    return [RuleSpec(field_key="pressure", field_name_cn="瓦斯压力", data_type="float",
        unit="MPa", value_kind="measured", strategy={"agent_extract": {"source_roles": ["report_body"]}})]


async def upload_fixture(sessions, *, broker_error=False, explicit_engine=None):
    source = b"%PDF-1.4 synthetic upload " + str(uuid4()).encode()
    filename = f"p19-upload-{uuid4()}.pdf"
    failure = RuntimeError("synthetic broker outage") if broker_error else None
    with (
        patch("app.api.parse.settings.app.reliable_enabled", True),
        patch("app.api.parse.rule_repository.list_checklist_types", AsyncMock(return_value=[{"checklist_type": "p19-upload"}])),
        patch("app.api.parse.rule_repository.get_rules_for_checklist", AsyncMock(return_value=rules())),
        patch("app.api.parse.minio_client.upload", return_value="mock-storage-only") as storage,
        patch("app.api.parse.minio_client.delete") as deletion,
        patch("app.tasks.reliable_content_build_task.apply_async", side_effect=failure) as dispatch,
        patch("app.api.parse.parse_document_task.delay") as legacy_dispatch,
    ):
        async with sessions() as db:
            response = await upload_and_parse(UploadFile(filename=filename, file=io.BytesIO(source)),
                "test-mine", "P19测试煤矿", "p19-upload", "2026-10",
                extraction_engine=explicit_engine, db=db)
        storage.assert_called_once()
        deletion.assert_not_called()
        dispatch.assert_called_once()
        legacy_dispatch.assert_not_called()
    assert response.status == "PREPARING_CONTENT"
    assert response.parse_run_id is None and response.build_id is not None
    assert response.contract_version == "reliable-extraction-v2"
    return response, source


@pytest.mark.asyncio
async def test_default_reliable_upload_commits_build_then_prepares_and_extracts(sessions):
    response, source = await upload_fixture(sessions)
    async with sessions() as db:
        document = await db.get(Document, response.document_id)
        build = await db.get(ContentBuildJob, response.build_id)
        assert document.file_hash == hashlib.sha256(source).hexdigest()
        assert build.document_id == document.document_id
        assert build.profile["contract_version"] == "reliable-extraction-v2"
        assert await db.scalar(select(func.count()).select_from(ParseRun).where(ParseRun.document_id == document.document_id)) == 0
        events = (await db.execute(select(ExecutionOutbox).where(
            ExecutionOutbox.kind == "content.build.execute", ExecutionOutbox.aggregate_id == response.build_id))).scalars().all()
        assert len(events) == 1 and events[0].dispatch_state == "pending"
    preparation = ContentPreparationService(parser=NativeOnePageParser(),
        builder=ContentBuildService(circuit=ServiceCircuit(MemoryCircuitStore())),
        cache_dir=Path(".tmp-p19-upload-cache") / str(uuid4()))
    result = await preparation.run_build(sessions, response.build_id, source_resolver=AsyncMock(return_value=source), max_regions=1)
    assert result["status"] == "published"
    run_id = UUID(result["run_id"])
    async with sessions() as db:
        run = await db.get(ParseRun, run_id)
        assert run.contract_version == "reliable-extraction-v2" and run.artifact_id is not None
        assert run.content_revision_id is not None
        assert await db.scalar(select(func.count()).select_from(ParseRun).where(ParseRun.document_id == response.document_id)) == 1
        jobs = (await db.execute(select(ParseFieldJob).where(ParseFieldJob.run_id == run_id))).scalars().all()
        assert len(jobs) == 1 and jobs[0].field_key == "pressure"
    provider = FakeProvider()
    executor = ReliableFieldExecutor(sessions,
        ModelGateway(provider, CallLedger(sessions), ServiceCircuit(MemoryCircuitStore())),
        ReliableFieldExecutorConfig("p19-upload-synthetic", "synthetic"))
    finished = await ReliableExtractionService().process_run(sessions, run_id, executor=executor,
        writer=executor.write_result, before_publish=executor.before_publish,
        owner="p19-upload", max_quantums=30, lease_seconds=300)
    assert finished["status"] == "COMPLETED" and provider.calls > 0
    async with sessions() as db:
        field = await db.scalar(select(ExtractedFieldModel).where(ExtractedFieldModel.parse_run_id == run_id))
        assert field.validation_status == "completed"
        assert {fact["value"] for fact in field.instances} == {"1.23"}
        assert field.extraction_detail["field_completion"]["coverage_complete"] is True


@pytest.mark.asyncio
async def test_broker_failure_preserves_uploaded_document_build_and_recovery_outbox(sessions):
    response, _source = await upload_fixture(sessions, broker_error=True, explicit_engine="reliable")
    async with sessions() as db:
        assert await db.get(Document, response.document_id) is not None
        assert await db.get(ContentBuildJob, response.build_id) is not None
        assert await db.scalar(select(func.count()).select_from(ParseRun).where(ParseRun.document_id == response.document_id)) == 0
        event = await db.scalar(select(ExecutionOutbox).where(ExecutionOutbox.kind == "content.build.execute",
                                                            ExecutionOutbox.aggregate_id == response.build_id))
        assert event.dispatch_state == "pending"


@pytest.mark.asyncio
async def test_disabled_explicit_reliable_upload_is_rejected_before_storage(sessions):
    with (
        patch("app.api.parse.settings.app.reliable_enabled", False),
        patch("app.api.parse.rule_repository.list_checklist_types", AsyncMock(return_value=[{"checklist_type": "p19-upload"}])),
        patch("app.api.parse.rule_repository.get_rules_for_checklist", AsyncMock(return_value=rules())),
        patch("app.api.parse.minio_client.upload") as storage,
        patch("app.tasks.reliable_content_build_task.apply_async") as dispatch,
    ):
        async with sessions() as db:
            with pytest.raises(HTTPException) as error:
                await upload_and_parse(UploadFile(filename="disabled.pdf", file=io.BytesIO(b"%PDF-1.4")),
                    "mine", "煤矿", "p19-upload", "2026-10", extraction_engine="reliable", db=db)
            assert error.value.status_code == 409
        storage.assert_not_called()
        dispatch.assert_not_called()
