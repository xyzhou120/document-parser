"""Explicit proxy inheritance for intranet and public model endpoints."""

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest

from app.agents.checklist import ChecklistAgent
from app.config import ModelSettings, Settings


def model(**overrides):
    return ModelSettings(base_url="http://model.test/v1", model_name="test-model", **overrides)


def test_model_proxy_defaults_and_yaml_environment_boolean_loading(monkeypatch):
    assert model().trust_env is True
    assert model(trust_env="false").trust_env is False
    default = {"models": {"vision_llm": {"base_url": "http://model.test/v1", "model_name": "test"}}}
    local = {"models": {"vision_llm": {"trust_env": False}}}
    monkeypatch.setattr(Settings, "_load_yaml", lambda self, path: default if path.name == "default.yaml"
                        else local if path.name == "local.yaml" else {})
    with patch.dict("os.environ", {}, clear=True):
        assert Settings().models["vision_llm"].trust_env is False
    with patch.dict("os.environ", {"VISION_LLM_TRUST_ENV": "true"}, clear=True):
        assert Settings().models["vision_llm"].trust_env is True


@pytest.mark.parametrize("frozen,local,expected", [
    ({"trust_env": False}, True, False),
    ({"trust_env": True}, False, True),
    ({}, False, False),
    ({}, True, True),
])
def test_checklist_client_uses_frozen_proxy_setting_then_local_default(frozen, local, expected):
    agent = ChecklistAgent(settings_override={"models": {"vision_llm": model(trust_env=local)}},
                           model_config={"base_url": "http://model.test/v1", "model_name": "test", **frozen})
    with patch("app.agents.checklist.httpx.AsyncClient") as transport, \
            patch("app.agents.checklist.AsyncOpenAI") as sdk:
        client, _, _, owns_client = agent._resolve_client()
    transport.assert_called_once_with(trust_env=expected)
    assert sdk.call_args.kwargs["http_client"] is transport.return_value
    assert client is sdk.return_value and owns_client


def test_checklist_old_frozen_model_uses_live_config_when_override_only_contains_limits():
    agent = ChecklistAgent(settings_override={"max_rounds": 2},
                           model_config={"base_url": "http://model.test/v1", "model_name": "test"})
    with patch("app.config.settings.models", {"vision_llm": model(trust_env=False)}), \
            patch("app.agents.checklist.httpx.AsyncClient") as transport, \
            patch("app.agents.checklist.AsyncOpenAI"):
        agent._resolve_client()
    transport.assert_called_once_with(trust_env=False)


def test_injected_checklist_client_does_not_create_transport():
    injected = object()
    agent = ChecklistAgent(settings_override={}, client=injected)
    with patch("app.agents.checklist.httpx.AsyncClient") as transport:
        client, _, _, owns_client = agent._resolve_client()
    assert client is injected and owns_client is False
    transport.assert_not_called()


@pytest.mark.parametrize("frozen,local,expected", [
    ({"trust_env": False}, True, False),
    ({"trust_env": True}, False, True),
    ({}, False, False),
    ({}, True, True),
])
@pytest.mark.parametrize("remaining", [0, 1])
def test_reliable_wakeup_selects_transport_and_closes_owned_client(frozen, local, expected, remaining):
    from app.tasks import reliable_execution_wakeup_task

    run = SimpleNamespace(contract_version="reliable-extraction-v2", status="RUNNING",
                          extraction_profile={"model_config": frozen})
    db = AsyncMock()
    db.get.return_value = run
    context = AsyncMock()
    context.__aenter__.return_value = db
    sessions = Mock(return_value=context)
    engine = Mock(dispose=AsyncMock())
    redis_client = Mock(aclose=AsyncMock())
    client = Mock(close=AsyncMock())
    with patch("app.tasks.settings.app.reliable_enabled", True), \
            patch("app.tasks.settings.models", {"vision_llm": model(trust_env=local)}), \
            patch("app.tasks.create_async_engine", return_value=engine), \
            patch("app.tasks.async_sessionmaker", return_value=sessions), \
            patch("redis.asyncio.from_url", return_value=redis_client), \
            patch("tempfile.TemporaryDirectory", return_value=nullcontext(".")), \
            patch("httpx.AsyncClient") as transport, \
            patch("openai.AsyncOpenAI", return_value=client) as sdk, \
            patch("app.services.reliable_extraction_service.reliable_extraction_service.process_run",
                  AsyncMock(return_value={"status": "RUNNING" if remaining else "COMPLETED",
                                         "remaining_runnable": remaining})), \
            patch("app.tasks.reliable_execution_dispatch_task.apply_async") as dispatch:
        result = reliable_execution_wakeup_task.run(str(uuid4()))
    assert result["status"] == ("RUNNING" if remaining else "COMPLETED")
    # 后续 outbox 等待独立调度；有剩余工作的量子也不能制造全局恢复消息。
    dispatch.assert_not_called()
    transport.assert_called_once_with(trust_env=expected)
    assert sdk.call_args.kwargs["http_client"] is transport.return_value
    client.close.assert_awaited_once()
    redis_client.aclose.assert_awaited_once()
    engine.dispose.assert_awaited_once()
