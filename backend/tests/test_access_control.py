import hashlib
import json
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI, Request

from app.security import AccessControlMiddleware, Principal, _required_scope, principal_for_key


def test_key_hash_resolves_actor_without_exposing_secret():
    secret = "unit-secret"
    config = [
        {
            "key_hash": hashlib.sha256(secret.encode()).hexdigest(),
            "actor": "robot",
            "scopes": ["read", "integration"],
        }
    ]
    with patch.dict("os.environ", {"PLATFORM_ACCESS_KEYS_JSON": json.dumps(config)}, clear=False):
        principal = principal_for_key(secret)
    assert principal == Principal("robot", frozenset({"read", "integration"}), authenticated=True)
    assert secret not in repr(principal)


def test_invalid_or_missing_auth_configuration_fails_closed_when_enabled():
    with patch.dict("os.environ", {"PLATFORM_ACCESS_KEYS_JSON": "not-json"}, clear=False):
        assert principal_for_key("anything") is None
    assert _required_scope("/api/v1/parse/runs/1", "GET") == "read"
    assert _required_scope("/api/v1/rules", "POST") == "rules:write"
    assert _required_scope("/api/v1/integrations/parse", "POST") == "integration"


def test_principal_scope_is_explicit_and_admin_is_the_only_wildcard():
    assert "integration" not in Principal("reader", frozenset({"read"})).scopes
    assert "admin" in Principal("admin", frozenset({"admin"})).scopes


def memory_app():
    app = FastAPI()
    app.add_middleware(AccessControlMiddleware)

    @app.get("/api/health")
    async def health():
        return {"status": "ok"}

    @app.post("/api/v1/rules")
    async def write_rule(request: Request):
        return await request.json()

    return app


@pytest.mark.asyncio
async def test_enabled_middleware_health_missing_key_wrong_scope_and_actor():
    key = "test-rule-writer"
    keys = [
        {
            "key_hash": hashlib.sha256(key.encode()).hexdigest(),
            "actor": "writer",
            "scopes": ["rules:write"],
        }
    ]
    with patch.dict(
        "os.environ",
        {"PLATFORM_AUTH_ENABLED": "true", "PLATFORM_ACCESS_KEYS_JSON": json.dumps(keys)},
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=memory_app()), base_url="https://memory.test"
        ) as client:
            assert (await client.get("/api/health")).status_code == 200
            assert (
                await client.post("/api/v1/rules", json={"operator": "writer"})
            ).status_code == 401
            wrong_actor = await client.post(
                "/api/v1/rules", headers={"X-API-Key": key}, json={"operator": "other"}
            )
            assert wrong_actor.status_code == 403
            matching = await client.post(
                "/api/v1/rules",
                headers={"Authorization": f"Bearer {key}"},
                json={"operator": "writer", "value": "unchanged"},
            )
            assert matching.status_code == 200
            assert matching.json()["value"] == "unchanged"  # Body survives middleware replay.
    keys[0]["scopes"] = ["read"]
    with patch.dict(
        "os.environ",
        {"PLATFORM_AUTH_ENABLED": "true", "PLATFORM_ACCESS_KEYS_JSON": json.dumps(keys)},
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=memory_app()), base_url="https://memory.test"
        ) as client:
            denied = await client.post(
                "/api/v1/rules", headers={"X-API-Key": key}, json={"operator": "writer"}
            )
            assert denied.status_code == 403


@pytest.mark.asyncio
async def test_disabled_middleware_keeps_current_local_workflow_and_bad_config_denies():
    with patch.dict("os.environ", {"PLATFORM_AUTH_ENABLED": "false"}):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=memory_app()), base_url="https://memory.test"
        ) as client:
            assert (
                await client.post("/api/v1/rules", json={"operator": "any-local-user"})
            ).status_code == 200
    with patch.dict(
        "os.environ", {"PLATFORM_AUTH_ENABLED": "true", "PLATFORM_ACCESS_KEYS_JSON": "broken"}
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=memory_app()), base_url="https://memory.test"
        ) as client:
            assert (
                await client.post("/api/v1/rules", headers={"X-API-Key": "anything"}, json={})
            ).status_code == 401
