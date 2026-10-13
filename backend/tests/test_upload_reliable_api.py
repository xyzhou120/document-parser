"""Upload routing must honor the local reliable switch before queueing work."""

import io
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI, UploadFile
from httpx import ASGITransport, AsyncClient

from app.api.parse import router, upload_and_parse
from app.services.reliable_extraction_service import ContentPreparationRequired
from app.storage import get_db


@pytest.fixture
def upload_context():
    db = Mock(add=Mock(), flush=AsyncMock(), commit=AsyncMock(), rollback=AsyncMock())

    async def assign_ids():
        for call in db.add.call_args_list:
            obj = call.args[0]
            if hasattr(obj, "document_id") and obj.document_id is None:
                obj.document_id = uuid4()
            if hasattr(obj, "parse_run_id") and obj.parse_run_id is None:
                obj.parse_run_id = uuid4()

    db.flush.side_effect = assign_ids
    db.commit.side_effect = assign_ids
    with (
        patch("app.api.parse.rule_repository.list_checklist_types", AsyncMock(return_value=[{"checklist_type": "gas"}])),
        patch("app.api.parse.rule_repository.get_rules_for_checklist", AsyncMock(return_value=[SimpleNamespace(methods=["agent_extract"])])),
        patch("app.api.parse.minio_client.upload", return_value="stored") as storage,
        patch("app.api.parse.parse_document_task.delay") as legacy,
        patch("app.tasks.reliable_content_build_task.apply_async") as build,
        patch("app.tasks.reliable_execution_wakeup_task.apply_async") as wake,
        patch("app.services.reliable_extraction_service.reliable_extraction_service.start", AsyncMock()) as start,
    ):
        yield SimpleNamespace(db=db, storage=storage, legacy=legacy, build=build, wake=wake, start=start)


async def submit(ctx, engine=None):
    return await upload_and_parse(
        UploadFile(filename="test.pdf", file=io.BytesIO(b"%PDF-1.4 synthetic")),
        "mine", "煤矿", "gas", "2026-10", extraction_engine=engine, db=ctx.db,
    )


@pytest.mark.asyncio
async def test_default_upload_goes_directly_to_content_build_when_reliable_enabled(upload_context):
    ctx = upload_context
    build_id = uuid4()
    ctx.start.side_effect = ContentPreparationRequired(build_id)

    def sent_after_commit(**kwargs):
        assert ctx.db.commit.await_count == 1

    ctx.build.side_effect = sent_after_commit
    with patch("app.api.parse.settings.app.reliable_enabled", True):
        response = await submit(ctx)
    assert response.status == "PREPARING_CONTENT"
    assert response.build_id == build_id and response.parse_run_id is None
    assert response.contract_version == "reliable-extraction-v2"
    ctx.legacy.assert_not_called()
    assert ctx.db.add.call_count == 1  # Only Document; no legacy run.
    ctx.build.assert_called_once_with(args=[str(build_id)], queue="reliable-execution")


@pytest.mark.asyncio
async def test_content_build_dispatch_failure_keeps_the_committed_upload(upload_context):
    ctx = upload_context
    ctx.start.side_effect = ContentPreparationRequired(uuid4())
    ctx.build.side_effect = ConnectionError("broker down")
    with patch("app.api.parse.settings.app.reliable_enabled", True):
        response = await submit(ctx, "reliable")
    assert response.build_id and response.status == "PREPARING_CONTENT"
    assert ctx.db.commit.await_count == 1
    ctx.db.rollback.assert_not_awaited()
    ctx.legacy.assert_not_called()


@pytest.mark.asyncio
async def test_ready_content_returns_actual_reliable_run_not_a_legacy_task(upload_context):
    ctx = upload_context
    run_id = uuid4()
    ctx.start.return_value = SimpleNamespace(parse_run_id=run_id, status="PLANNED")
    with patch("app.api.parse.settings.app.reliable_enabled", True):
        response = await submit(ctx, "reliable")
    assert response.parse_run_id == run_id
    assert response.extraction_engine == "reliable"
    ctx.wake.assert_called_once_with(args=[str(run_id)], queue="reliable-execution")
    ctx.legacy.assert_not_called()
    ctx.build.assert_not_called()


@pytest.mark.asyncio
async def test_explicit_reliable_is_rejected_before_upload_when_disabled(upload_context):
    from fastapi import HTTPException

    with patch("app.api.parse.settings.app.reliable_enabled", False):
        with pytest.raises(HTTPException) as exc:
            await submit(upload_context, "reliable")
    assert exc.value.status_code == 409
    upload_context.storage.assert_not_called()
    upload_context.start.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled,engine", [(False, None), (True, "checklist_agent")])
async def test_legacy_default_or_explicit_choice_still_uses_legacy(upload_context, enabled, engine):
    with (
        patch("app.api.parse.settings.app.reliable_enabled", enabled),
        patch("app.api.parse.rule_snapshot", return_value={"extraction_engine": "checklist_agent"}),
    ):
        response = await submit(upload_context, engine)
    assert response.parse_run_id and response.extraction_engine == "checklist_agent"
    upload_context.legacy.assert_called_once()
    upload_context.start.assert_not_awaited()


@pytest.mark.asyncio
async def test_capabilities_and_http_response_union_do_not_label_legacy_as_reliable(upload_context):
    ctx = upload_context
    app = FastAPI()
    app.include_router(router)

    async def database():
        yield ctx.db

    app.dependency_overrides[get_db] = database
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        with patch("app.api.parse.settings.app.reliable_enabled", True):
            capabilities = (await client.get("/api/v1/parse/capabilities")).json()
            assert capabilities["default_extraction_engine"] == "reliable"
            ctx.start.side_effect = ContentPreparationRequired(uuid4())
            reliable = await client.post("/api/v1/parse/upload-and-parse", files={"file": ("t.pdf", b"%PDF-1.4")},
                data={"mine_id": "m", "mine_name": "矿", "checklist_type": "gas", "reporting_period": "2026-10"})
            assert reliable.status_code == 200
            assert reliable.json()["contract_version"] == "reliable-extraction-v2"
        with (
            patch("app.api.parse.settings.app.reliable_enabled", False),
            patch("app.api.parse.rule_snapshot", return_value={}),
        ):
            legacy = await client.post("/api/v1/parse/upload-and-parse", files={"file": ("t.pdf", b"%PDF-1.4")},
                data={"mine_id": "m", "mine_name": "矿", "checklist_type": "gas", "reporting_period": "2026-10"})
            assert legacy.status_code == 200
            assert legacy.json()["parse_run_id"]
            assert "contract_version" not in legacy.json()
