"""可选启用的 API Key 访问控制。

认证关闭时平台仍便于本地开发。启用后，缺失或格式错误的凭据会失败关闭，
密钥不会出现在日志或响应中。
"""

import hashlib
import hmac
import json
import os
import re
from dataclasses import dataclass

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

SCOPES = {"read", "parse", "rules:write", "reviews:write", "source:write", "integration", "admin"}


@dataclass(frozen=True)
class Principal:
    actor: str
    scopes: frozenset[str]
    authenticated: bool = False


def _auth_enabled() -> bool:
    # 配置值拼写错误时不能静默关闭认证。
    return os.getenv("PLATFORM_AUTH_ENABLED", "false").strip().lower() not in {"0", "false", "no", "off", ""}


def _configured_keys() -> list[dict]:
    raw = os.getenv("PLATFORM_ACCESS_KEYS_JSON", "")
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return []
    rows = (
        value
        if isinstance(value, list)
        else list(value.values()) if isinstance(value, dict) else []
    )
    valid = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            return []
        key_hash = row.get("key_hash") or row.get("sha256") or ""
        actor, scopes = row.get("actor"), row.get("scopes")
        if (
            not isinstance(key_hash, str)
            or not re.fullmatch(r"[0-9a-fA-F]{64}", key_hash)
            or not isinstance(actor, str)
            or not actor.strip()
            or len(actor) > 200
            or not isinstance(scopes, list)
            or any(not isinstance(scope, str) or scope not in SCOPES for scope in scopes)
            or key_hash.lower() in seen
        ):
            return []
        seen.add(key_hash.lower())
        valid.append({**row, "actor": actor.strip()})
    return valid


def _api_key(request: Request) -> str | None:
    value = request.headers.get("x-api-key")
    if value:
        return value.strip()
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


def principal_for_key(key: str | None) -> Principal | None:
    if not key:
        return None
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    for row in _configured_keys():
        configured = str(row.get("key_hash") or row.get("sha256") or "").lower()
        if not configured or not hmac.compare_digest(configured, digest):
            continue
        scopes = frozenset(str(scope) for scope in row.get("scopes", []) if str(scope) in SCOPES)
        return Principal(str(row.get("actor") or "unknown"), scopes, authenticated=True)
    return None


def get_principal(request: Request) -> Principal:
    principal = getattr(request.state, "principal", None)
    if isinstance(principal, Principal):
        return principal
    if not _auth_enabled():
        return Principal("anonymous", frozenset(SCOPES), authenticated=False)
    raise HTTPException(status_code=401, detail="需要有效的 API 凭据")


def require_scope(request: Request, scope: str) -> Principal:
    principal = get_principal(request)
    if scope not in SCOPES:
        raise HTTPException(status_code=500, detail="服务端 scope 配置无效")
    if scope not in principal.scopes and "admin" not in principal.scopes:
        raise HTTPException(status_code=403, detail="当前凭据没有执行此操作的权限")
    return principal


def assert_actor(principal: Principal, actor: str | None) -> None:
    """防止已认证请求写入其他操作员的身份。"""
    if principal.authenticated and actor and actor.strip() != principal.actor:
        raise HTTPException(status_code=403, detail="提交身份必须与访问凭据的 actor 一致")


def _required_scope(path: str, method: str) -> str:
    if path in {"/health", "/api/health", "/docs", "/redoc", "/openapi.json"}:
        return "read"
    if path.startswith("/api/v1/integrations"):
        return "integration"
    if path.startswith("/api/v1/reviews"):
        return "reviews:write" if method not in {"GET", "HEAD", "OPTIONS"} else "read"
    if path.startswith("/api/v1/artifacts"):
        return "source:write" if method not in {"GET", "HEAD", "OPTIONS"} else "read"
    if path.startswith("/api/v1/rule-releases") or path.startswith("/api/v1/rules"):
        return "rules:write" if method not in {"GET", "HEAD", "OPTIONS"} else "read"
    if (
        path.startswith("/api/v1/parse")
        or path.startswith("/api/v1/batches")
        or path.startswith("/api/v1/documents")
        or path.startswith("/api/v1/imports")
        or path.startswith("/api/v1/attachments")
    ):
        return "parse" if method not in {"GET", "HEAD", "OPTIONS"} else "read"
    return "read" if method in {"GET", "HEAD", "OPTIONS"} else "admin"


class AccessControlMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if (
            request.url.path in {"/health", "/api/health"}
            or not request.url.path.startswith("/api/")
            or request.method == "OPTIONS"
        ):
            return await call_next(request)
        if not _auth_enabled():
            request.state.principal = Principal("anonymous", frozenset(SCOPES), authenticated=False)
            return await call_next(request)
        principal = principal_for_key(_api_key(request))
        if principal is None:
            return JSONResponse({"detail": "需要有效的 API 凭据"}, status_code=401)
        scope = _required_scope(request.url.path, request.method)
        if scope not in principal.scopes and "admin" not in principal.scopes:
            return JSONResponse({"detail": "当前凭据没有执行此操作的权限"}, status_code=403)
        request.state.principal = principal
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
            if content_type == "application/json" or content_type.endswith("+json"):
                try:
                    body = await request.json()
                except (ValueError, UnicodeDecodeError):
                    return JSONResponse({"detail": "请求 JSON 格式无效"}, status_code=400)
                if isinstance(body, dict):
                    for key in ("actor", "operator", "reviewer", "created_by"):
                        value = body.get(key)
                        if value is not None and (
                            not isinstance(value, str) or value.strip() != principal.actor
                        ):
                            return JSONResponse(
                                {"detail": "提交身份必须与访问凭据的 actor 一致"}, status_code=403
                            )
        return await call_next(request)
