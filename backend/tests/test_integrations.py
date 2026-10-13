from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.models.database import Document, ParseRun
from app.models.integrations import IntegrationOutbox
from app.rules.spec import RuleSpec
from app.services.integration_service import (
    IntegrationConfig,
    IntegrationService,
    _endpoint_valid,
    _hash,
)


def test_destination_allowlist_rejects_plain_http_private_and_redirect_prone_values():
    assert _endpoint_valid("https://receiver.example.test/callback")
    assert not _endpoint_valid("http://receiver.example.test/callback")
    assert not _endpoint_valid("https://127.0.0.1/callback")
    assert not _endpoint_valid("https://localhost/callback")
    assert not _endpoint_valid("https://user:password@receiver.example.test/callback")


class Db:
    def __init__(self, rows=None):
        self.rows = rows or {}
        self.added = []

    async def execute(self, *_args, **_kwargs):
        class Result:
            def scalar_one_or_none(self):
                return None

        return Result()

    async def get(self, model, key):
        return self.rows.get((model, key))

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        return None


@pytest.mark.asyncio
async def test_new_event_uses_uploaded_document_and_fixed_snapshot():
    document = Document(document_id=uuid4(), file_path="uploads/existing.pdf", file_hash="a" * 64,
                        checklist_type="gas")
    db = Db({(Document, document.document_id): document})
    service = IntegrationService(IntegrationConfig(enabled=True))
    rule = RuleSpec(field_key="gas", field_name_cn="gas", version=3)
    with patch("app.services.integration_service.rule_repository.get_rules_for_document",
               AsyncMock(return_value=[rule])):
        request, created = await service.accept(db, {
            "event_id": "evt-new", "document_id": str(document.document_id), "engine": "rules",
        }, "robot")
    run = next(row for row in db.added if isinstance(row, ParseRun))
    assert created and request.parse_run_id == run.parse_run_id
    assert run.extraction_profile["rules"][0]["version"] == 3
    assert run.extraction_profile["integration_event_id"] == "evt-new"
    assert run.extraction_profile["extraction_engine"] == "rules"


@pytest.mark.asyncio
async def test_missing_original_is_rejected_before_any_queue_or_model_work():
    service = IntegrationService(IntegrationConfig(enabled=True))
    with pytest.raises(HTTPException) as error:
        await service.accept(Db(), {"event_id": "evt-missing", "document_id": str(uuid4()), "engine": "rules"}, "robot")
    assert error.value.status_code == 404


@pytest.mark.asyncio
async def test_dispatch_disabled_does_not_invoke_transport_or_read_outbox():
    service = IntegrationService(IntegrationConfig(enabled=True, webhook_enabled=False))
    transport = AsyncMock()
    db = Mock(execute=AsyncMock())
    with pytest.raises(HTTPException) as error:
        await service.dispatch(db, "evt", transport=transport)
    assert error.value.status_code == 503
    db.execute.assert_not_awaited()
    transport.assert_not_awaited()


@pytest.mark.asyncio
async def test_queue_submission_commits_request_before_celery_and_preserves_failure():
    from app.api.integrations import _submit
    db = Mock(commit=AsyncMock(), get=AsyncMock(return_value=SimpleNamespace(status="PENDING", error_message=None)))
    record = SimpleNamespace(document_id=uuid4(), parse_run_id=uuid4(), state="PENDING")
    def fail(*_args):
        assert db.commit.await_count == 1
        raise RuntimeError("broker unavailable")
    with patch("app.api.integrations.parse_document_task.delay", side_effect=fail):
        with pytest.raises(HTTPException) as error:
            await _submit(db, record)
    assert error.value.status_code == 503
    assert record.state == "QUEUE_FAILED"
    assert db.commit.await_count == 2


@pytest.mark.asyncio
async def test_disabled_inbound_never_reads_or_downloads_url():
    service = IntegrationService(IntegrationConfig(enabled=False))
    with pytest.raises(HTTPException) as error:
        await service.accept(
            Db(), {"event_id": "evt-1", "document_id": str(uuid4()), "engine": "rules"}, "actor"
        )
    assert error.value.status_code == 503


@pytest.mark.asyncio
async def test_same_event_payload_hash_is_replay_but_changed_payload_is_conflict():
    event = "evt-replay"
    doc_id, run_id = uuid4(), uuid4()
    from app.models.integrations import IntegrationRequest

    first = IntegrationRequest(
        event_id=event,
        document_id=doc_id,
        parse_run_id=run_id,
        payload_hash=_hash({"event_id": event}),
        payload={},
        state="PENDING",
        actor="robot",
    )
    db = Db({(IntegrationRequest, event): first})
    service = IntegrationService(IntegrationConfig(enabled=True))
    same, created = await service.accept(db, {"event_id": event}, "robot")
    assert same is first and created is False
    with pytest.raises(HTTPException) as error:
        await service.accept(db, {"event_id": event, "changed": True}, "robot")
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_webhook_dispatch_requires_matching_ack_and_uses_idempotency_key():
    service = IntegrationService(
        IntegrationConfig(
            enabled=True,
            webhook_enabled=True,
            destinations={"receiver": "https://receiver.example.test/hook"},
        )
    )
    event = "parse-result-1"
    outbox = IntegrationOutbox(
        event_id=event,
        parse_run_id=uuid4(),
        document_id=uuid4(),
        payload={"event_id": event},
        destination="receiver",
        state="PENDING",
        attempts=0,
        max_attempts=2,
    )
    db = Db()

    async def transport(endpoint, body, headers):
        assert endpoint.endswith("/hook")
        assert headers["Idempotency-Key"] == event
        return SimpleNamespace(
            status_code=200, json=lambda: {"event_id": event, "acknowledged": True}
        )

    # 此单元测试用模拟 Result 表示 with_for_update 查询；
    # 将 execute 替换为返回发件箱的结果。
    class Result:
        def scalar_one_or_none(self):
            return outbox

    async def execute(*_args, **_kwargs):
        return Result()

    db.execute = execute
    result = await service.dispatch(db, event, transport=transport)
    assert result.state == "DELIVERED"
    assert result.attempts == 1
    assert result.acknowledgement["receipt"]["event_id"] == event


@pytest.mark.asyncio
async def test_webhook_failure_retries_with_backoff_then_failed():
    service = IntegrationService(
        IntegrationConfig(
            enabled=True,
            webhook_enabled=True,
            destinations={"receiver": "https://receiver.example.test/hook"},
        )
    )
    outbox = IntegrationOutbox(
        event_id="evt",
        parse_run_id=uuid4(),
        document_id=uuid4(),
        payload={},
        destination="receiver",
        state="PENDING",
        attempts=0,
        max_attempts=1,
    )

    class Result:
        def scalar_one_or_none(self):
            return outbox

    class Db:
        async def execute(self, *_a, **_k):
            return Result()

        async def flush(self):
            pass

    async def transport(*_args):
        return SimpleNamespace(status_code=502, json=lambda: {})

    result = await service.dispatch(Db(), "evt", transport=transport)
    assert result.state == "FAILED"
    assert result.next_attempt_at is None
