from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.reliable_execution import execution, pause, resume, resume_content_build, router
from app.services.reliable_extraction_service import CONTRACT_VERSION
from app.storage import get_db


@pytest.fixture(autouse=True)
def enable_reliable():
    with patch("app.api.reliable_execution.settings.app.reliable_enabled", True):
        yield


@pytest.mark.asyncio
async def test_old_run_is_read_only_legacy_compatible():
    run = SimpleNamespace(parse_run_id=uuid4(), document_id=uuid4(), status="COMPLETED",
                          contract_version=None, execution_generation=0)
    db = Mock(get=AsyncMock(return_value=run))
    result = await execution(run.parse_run_id, limit=100, db=db)
    assert result["legacy"] is True
    assert result["contract_version"] == "legacy-compatible-read"
    assert result["automatic_complete"] is False


@pytest.mark.asyncio
async def test_v2_status_reads_real_jobs_and_does_not_claim_without_ledger():
    run = SimpleNamespace(parse_run_id=uuid4(), document_id=uuid4(), status="RUNNING",
                          contract_version=CONTRACT_VERSION, execution_generation=2)
    job = SimpleNamespace(job_id=uuid4(), run_id=run.parse_run_id, field_key="gas",
                          rule_hash="a" * 64, revision_id=uuid4(), execution_state="yielded",
                          semantic_state="unknown", phase="yielded", checkpoint={"cursor": 3},
                          next_attempt_at=None, lease_owner=None, lease_until=None, fence_token=1,
                          progress_seq=2, attempt=1, last_error=None)
    db = Mock(get=AsyncMock(return_value=run), execute=AsyncMock(side_effect=[
        Mock(scalars=Mock(return_value=Mock(all=Mock(return_value=[job])))),
        Mock(scalars=Mock(return_value=Mock(all=Mock(return_value=[job])))),
    ]))
    result = await execution(run.parse_run_id, limit=100, db=db)
    assert result["status"] == "YIELDED"
    assert result["automatic_complete"] is False
    assert result["jobs"][0].field_key == "gas"


@pytest.mark.asyncio
async def test_control_endpoint_delegates_to_database_service():
    run_id = uuid4()
    with patch("app.api.reliable_execution.reliable_extraction_service.control",
               AsyncMock(return_value={"contract_version": CONTRACT_VERSION, "run_id": run_id,
                                       "status": "PAUSED", "execution_generation": 3,
                                       "resumed": False})) as control:
        result = await pause(run_id, db=Mock())
    assert result["status"] == "PAUSED"
    control.assert_awaited_once()


@pytest.mark.asyncio
async def test_resume_control_requires_service_result_and_preserves_retry_flag():
    run_id = uuid4()
    with patch("app.api.reliable_execution.reliable_extraction_service.control",
               AsyncMock(return_value={"contract_version": CONTRACT_VERSION, "run_id": run_id,
                                       "status": "RUNNING", "execution_generation": 4,
                                       "resumed": True})) as control:
        result = await resume(run_id, retry_failed=True, db=Mock())
    assert result["resumed"] is True
    assert control.await_args.kwargs["retry_failed"] is True


@pytest.mark.asyncio
async def test_http_legacy_reads_but_control_returns_409():
    run = SimpleNamespace(parse_run_id=uuid4(), document_id=uuid4(), status="COMPLETED",
                          contract_version=None, execution_generation=0)
    db = Mock(get=AsyncMock(return_value=run))
    app = FastAPI()
    app.include_router(router)

    async def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/v1/parse/runs/{run.parse_run_id}/execution")
        assert response.status_code == 200
        assert response.json()["legacy"] is True
        assert response.json()["automatic_complete"] is False
        control = await client.post(f"/api/v1/parse/runs/{run.parse_run_id}/resume")
        assert control.status_code == 409


@pytest.mark.asyncio
async def test_http_feature_flag_and_uuid_validation():
    app = FastAPI()
    app.include_router(router)

    async def override_db():
        yield Mock()

    app.dependency_overrides[get_db] = override_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        invalid = await client.get("/api/v1/parse/runs/invalid/execution")
        assert invalid.status_code == 422
        with patch("app.api.reliable_execution.settings.app.reliable_enabled", False):
            disabled = await client.get(f"/api/v1/parse/runs/{uuid4()}/execution")
        assert disabled.status_code == 404


@pytest.mark.asyncio
async def test_content_resume_is_idempotent_for_published_build():
    document_id, build_id = uuid4(), uuid4()
    build = SimpleNamespace(document_id=document_id, phase="published", checkpoint={"parse_run_id": str(uuid4())})
    result = await resume_content_build(document_id, build_id, db=Mock(get=AsyncMock(return_value=build)))
    assert result["status"] == "CONTENT_READY"


@pytest.mark.asyncio
async def test_content_resume_rejects_a_build_from_another_document():
    from fastapi import HTTPException
    build = SimpleNamespace(document_id=uuid4(), phase="partial", checkpoint={})
    with pytest.raises(HTTPException) as error:
        await resume_content_build(uuid4(), uuid4(), db=Mock(get=AsyncMock(return_value=build)))
    assert error.value.status_code == 404
