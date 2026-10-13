"""Rule patch/import and dependency handling regressions; no network/database writes."""

import os
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.api import rules, services
from app.config import Settings
from app.models.rules import ExtractionRule
from app.rules.repository import RuleRepository
from app.schemas.rules import RulePatch
from app.storage.minio_client import MinIOClient


def test_patch_distinguishes_omission_from_explicit_null():
    assert RulePatch().model_dump(exclude_unset=True) == {}
    assert RulePatch(unit=None, fallback_value=None).model_dump(exclude_unset=True) == {
        "unit": None, "fallback_value": None,
    }
    for field in ("required", "priority", "strategy", "field_name_cn", "field_key", "enabled"):
        with pytest.raises(ValidationError):
            RulePatch(**{field: None})
    with pytest.raises(ValidationError):
        RulePatch(priority=101)


@pytest.mark.asyncio
async def test_rule_repository_clears_nullable_fields_without_resetting_strategy():
    rule = ExtractionRule(rule_id=uuid4(), checklist_type="alpha", field_key="gas", field_name_cn="瓦斯",
                          unit="MPa", fallback_value="0.74", field_description="旧说明", version=1,
                          strategy={"regex": {"pattern": "original"}}, validation={"min": 0})
    db = Mock(get=AsyncMock(return_value=rule), flush=AsyncMock(), commit=AsyncMock())
    repository = RuleRepository()
    repository._log_change = AsyncMock()
    await repository.update_rule(db, rule.rule_id, {"unit": None, "fallback_value": None,
                                                   "field_description": None, "validation": None})
    assert rule.unit is rule.fallback_value is rule.field_description is rule.validation is None
    assert rule.strategy == {"regex": {"pattern": "original"}}
    assert rule.version == 2


@pytest.mark.asyncio
async def test_parser_reads_current_rules_and_rejects_inactive_checklist():
    repository = RuleRepository()
    rule = ExtractionRule(rule_id=uuid4(), checklist_type="alpha", field_key="gas", field_name_cn="新规则",
                          strategy={}, version=2)
    repository._spec_cache["alpha"] = (float("inf"), ["stale spec"])
    repository.list_rules = AsyncMock(return_value=[rule])
    db = Mock(get=AsyncMock(return_value=SimpleNamespace(is_active=True)))
    current = await repository.get_rules_for_checklist(db, "alpha")
    assert current[0].field_name_cn == "新规则"
    assert current[0].version == 2
    db.get.return_value = SimpleNamespace(is_active=False)
    assert await repository.get_rules_for_checklist(db, "alpha") == []
    repository.list_rules.assert_awaited_once()


@pytest.mark.asyncio
async def test_import_rejects_invalid_row_and_continues_next_row():
    repository = RuleRepository()
    repository.create_rule = AsyncMock()
    db = Mock(execute=AsyncMock(return_value=Mock(scalar_one_or_none=Mock(return_value=None))),
              rollback=AsyncMock())
    result = await repository.bulk_import(db, "alpha", [
        {"field_key": "invalid", "field_name_cn": "错误", "data_type": "unknown"},
        {"field_key": "valid", "field_name_cn": "正确", "strategy": {}},
    ])
    assert result["created"] == 1
    assert len(result["failed"]) == 1
    assert repository.create_rule.await_args.args[1]["field_key"] == "valid"


@pytest.mark.asyncio
async def test_import_file_size_is_bounded_before_decode():
    file = Mock(filename="rules.json", read=AsyncMock(return_value=b"x" * (5 * 1024 * 1024 + 1)))
    db = Mock(get=AsyncMock(return_value=object()))
    with pytest.raises(rules.HTTPException) as exc:
        await rules.import_rules_file(checklist_type="alpha", file=file, db=db)
    assert exc.value.status_code == 413
    file.read.assert_awaited_once_with(5 * 1024 * 1024 + 1)


def test_environment_overrides_yaml_without_mutating_environment():
    data = {
        "database": {"host": "yaml.invalid", "password": "yaml-password"},
        "environment": {"OPENAI_API_KEY": "yaml-key"},
        "models": {"text_llm": {"base_url": "http://yaml.invalid", "model_name": "model"}},
    }
    environment = {"DATABASE_HOST": "env.invalid", "DATABASE_PASSWORD": "p@ss:word",
                   "REDIS_HOST": "redis.invalid", "CELERY_BROKER_URL": "",
                   "CELERY_RESULT_BACKEND": "", "OPENAI_API_KEY": "env-key",
                   "TEXT_LLM_BASE_URL": "http://llm.invalid/v1"}
    with patch.dict(os.environ, environment, clear=True), patch.object(Settings, "_load_yaml", return_value=data):
        before = dict(os.environ)
        configuration = Settings()
        assert configuration.database.host == "env.invalid"
        assert "p%40ss%3Aword" in configuration.database.url
        assert configuration.celery.broker_url == configuration.redis.url
        assert configuration.celery.result_backend == configuration.redis.url
        assert configuration.models["text_llm"].api_key == "env-key"
        assert configuration.models["text_llm"].base_url == "http://llm.invalid/v1"
        assert dict(os.environ) == before


def test_minio_initialization_is_lazy_and_presign_uses_timedelta():
    with patch("app.storage.minio_client.Minio") as constructor:
        client = MinIOClient()
        constructor.assert_not_called()
        client.get_url("sample.pdf", expires=120)
        constructor.return_value.presigned_get_object.assert_called_once_with(
            client.bucket, "sample.pdf", expires=timedelta(seconds=120)
        )
        constructor.return_value.bucket_exists.assert_not_called()


def test_minio_download_releases_response_when_read_fails():
    client = MinIOClient()
    response = Mock(read=Mock(side_effect=OSError("read failed")))
    client._client = Mock(get_object=Mock(return_value=response))
    with pytest.raises(OSError):
        client.download("sample.pdf")
    response.close.assert_called_once()
    response.release_conn.assert_called_once()


@pytest.mark.asyncio
async def test_worker_online_requires_actual_ping_reply():
    with patch("app.celery_app.celery_app.control.inspect") as inspect:
        inspect.return_value.ping.return_value = None
        assert (await services._check_worker())["status"] == "offline"
        inspect.return_value.ping.return_value = {"worker@host": {"ok": "pong"}}
        assert (await services._check_worker())["status"] == "online"


@pytest.mark.asyncio
async def test_seed_deletions_are_not_resurrected_on_restart():
    from app.rules.seeding import seed_rules

    history = [SimpleNamespace(checklist_type="alpha", action="delete", before_state={"field_key": "gas"})]
    db = Mock(
        execute=AsyncMock(return_value=Mock(scalars=Mock(return_value=Mock(all=Mock(return_value=history))))),
        get=AsyncMock(return_value=object()), scalar=AsyncMock(return_value=0),
        flush=AsyncMock(), commit=AsyncMock(), add=Mock(),
    )
    with patch("app.rules.seeding.CHECKLIST_TYPES", [{"checklist_type": "alpha", "checklist_name": "A"}]), \
         patch("app.rules.seeding.RULES", [{"checklist_type": "alpha", "field_key": "gas", "field_name_cn": "瓦斯"}]):
        response = await seed_rules(db)
    assert response["rules_created"] == 0
    assert response["rules_skipped"] == 1
    db.add.assert_not_called()


def test_tasks_retry_only_transient_errors_and_exhaust_to_failed():
    import app.tasks as tasks

    def engine():
        session = Mock(__aenter__=AsyncMock(return_value=Mock()), __aexit__=AsyncMock(return_value=False))
        return Mock(dispose=AsyncMock()), Mock(return_value=session)

    created_engine, session_factory = engine()
    task = tasks.parse_document_task
    run_id = uuid4()
    with patch.object(tasks, "create_async_engine", return_value=created_engine), \
         patch.object(tasks, "async_sessionmaker", return_value=session_factory), \
         patch.object(tasks, "_set_run_status", new_callable=AsyncMock) as status, \
         patch.object(tasks, "ParseService") as service:
        service.return_value.parse_document = AsyncMock(side_effect=ValueError("invalid PDF"))
        with pytest.raises(ValueError):
            task.run(str(uuid4()), str(run_id))
        status.assert_awaited_once_with(run_id, "FAILED", "invalid PDF")
        created_engine.dispose.assert_awaited()

        status.reset_mock()
        service.return_value.parse_document.side_effect = ConnectionError("temporary outage")
        task.push_request(retries=task.max_retries)
        try:
            with pytest.raises(ConnectionError):
                task.run(str(uuid4()), str(run_id))
        finally:
            task.pop_request()
        status.assert_awaited_once_with(run_id, "FAILED", "temporary outage")

        status.reset_mock()
        task.push_request(retries=0)
        try:
            with patch.object(task, "retry", side_effect=RuntimeError("scheduled")) as retry:
                with pytest.raises(RuntimeError, match="scheduled"):
                    task.run(str(uuid4()), str(run_id))
                assert retry.call_args.kwargs["countdown"] == 60
        finally:
            task.pop_request()
        status.assert_awaited_once_with(run_id, "RETRYING", "temporary outage")


def test_invalid_document_uuid_records_failed_for_valid_run():
    import app.tasks as tasks

    run_id = uuid4()
    with patch.object(tasks, "_set_run_status", new_callable=AsyncMock) as status, \
         patch.object(tasks, "ParseService") as service, \
         patch.object(tasks.parse_document_task, "retry") as retry:
        with pytest.raises(ValueError):
            tasks.parse_document_task.run("invalid-uuid", str(run_id))
        assert status.await_args.args[:2] == (run_id, "FAILED")
        retry.assert_not_called()
        service.assert_not_called()
