"""Opt-in protocol adapter and durable outbox; no broker or SSO is assumed."""

import hashlib
import hmac
import ipaddress
import json
import os
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
from fastapi import HTTPException
from sqlalchemy import select, text

from app.models.database import Document, ExtractedFieldModel, ParseRun
from app.models.integrations import IntegrationOutbox, IntegrationRequest, utcnow
from app.rules.repository import rule_repository
from app.services.fact_service import build_fact_view
from app.services.parse_service import rule_snapshot
from app.services.release_service import bundle_specs, release_service, validate_bundle


def _enabled(name):
    return os.getenv(name, "false").strip().lower() in {"true", "1", "yes", "on"}


def _endpoint_valid(endpoint):
    if not isinstance(endpoint, str):
        return False
    try:
        parsed = urlsplit(endpoint)
        host = parsed.hostname or ""
        if (
            parsed.scheme != "https"
            or not host
            or parsed.username
            or parsed.password
            or parsed.fragment
            or host.lower() in {"localhost", "localhost.localdomain"}
            or host.lower().endswith((".local", ".localhost", ".internal"))
        ):
            return False
        try:
            return ipaddress.ip_address(host).is_global
        except ValueError:
            return "." in host
    except ValueError:
        return False


@dataclass(frozen=True)
class IntegrationConfig:
    enabled: bool = False
    webhook_enabled: bool = False
    destinations: dict[str, str] = field(default_factory=dict)
    signing_secret: str | None = field(default=None, repr=False)

    @classmethod
    def from_env(cls):
        try:
            destinations = json.loads(os.getenv("PLATFORM_WEBHOOK_DESTINATIONS_JSON", "{}"))
        except json.JSONDecodeError:
            destinations = {}
        if not isinstance(destinations, dict) or any(
            not isinstance(name, str)
            or not name.strip()
            or len(name) > 100
            or not _endpoint_valid(endpoint)
            for name, endpoint in destinations.items()
        ):
            destinations = {}
        return cls(
            enabled=_enabled("PLATFORM_INTEGRATION_ENABLED"),
            webhook_enabled=_enabled("PLATFORM_WEBHOOK_ENABLED"),
            destinations=destinations,
            signing_secret=os.getenv("PLATFORM_WEBHOOK_SIGNING_SECRET") or None,
        )


def _hash(payload):
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


async def _event_lock(db, event_id):
    # Cross-document event replays also serialize; unique PK remains the last guard.
    key = int.from_bytes(hashlib.sha256(event_id.encode()).digest()[:8], "big", signed=True)
    await db.execute(text("SELECT pg_advisory_xact_lock(:event_lock)"), {"event_lock": key})


class IntegrationService:
    def __init__(self, config: IntegrationConfig | None = None):
        self._config = config

    @property
    def config(self):
        return self._config or IntegrationConfig.from_env()

    async def accept(self, db, payload: dict, actor: str):
        config = self.config
        if not config.enabled:
            raise HTTPException(503, "外部接入尚未启用")
        event_id = payload["event_id"]
        await _event_lock(db, event_id)
        payload_hash = _hash(payload)
        existing = await db.get(IntegrationRequest, event_id)
        if existing:
            if existing.actor != actor:
                raise HTTPException(403, "接入事件属于其他 actor")
            if existing.payload_hash != payload_hash:
                raise HTTPException(409, "同一 event_id 的输入发生变化，请使用新 event_id")
            return existing, False
        document = await db.get(Document, UUID(payload["document_id"]))
        if document is None or not document.file_path or not document.file_hash:
            raise HTTPException(404, "已上传原文不存在，请先完成平台上传")
        destination = payload.get("destination")
        if destination and destination not in config.destinations:
            raise HTTPException(422, "回调目的地未列入本地允许列表")
        if payload.get("rule_bundle_id"):
            bundle = await release_service.get(db, UUID(payload["rule_bundle_id"]))
            await release_service._checklist(db, document.checklist_type)
            if bundle.checklist_type != document.checklist_type:
                raise HTTPException(422, "规则 bundle 与文档清单不一致")
            if not validate_bundle(bundle)["valid"]:
                raise HTTPException(409, "规则 bundle 校验失败")
            rules = bundle_specs(bundle)
        else:
            rules = await rule_repository.get_rules_for_document(db, document)
        if not rules:
            raise HTTPException(422, "该文档清单没有启用规则")
        if payload["engine"] == "rules" and any(rule.methods == ["agent_extract"] for rule in rules):
            raise HTTPException(422, "此规则包包含智能体专用字段，请指定 checklist_agent")
        profile = rule_snapshot(rules, document.checklist_type, payload["engine"])
        if payload.get("rule_bundle_id"):
            profile["rule_bundle_id"] = payload["rule_bundle_id"]
        run = ParseRun(
            parse_run_id=uuid4(),
            document_id=document.document_id,
            checklist_type=document.checklist_type,
            status="PENDING",
            run_mode="FULL",
            extraction_profile={
                **profile,
                "integration_event_id": event_id,
                "integration_destination": destination,
                "force_refresh": payload.get("force_refresh", False),
            },
        )
        request = IntegrationRequest(
            event_id=event_id,
            document_id=document.document_id,
            parse_run_id=run.parse_run_id,
            payload_hash=payload_hash,
            payload=deepcopy(payload),
            state="PENDING",
            actor=actor,
        )
        db.add(run)
        db.add(request)
        await db.flush()
        return request, True

    async def result(self, db, event_id):
        request = await db.get(IntegrationRequest, event_id)
        if request is None:
            raise HTTPException(404, "接入事件不存在")
        run = await db.get(ParseRun, request.parse_run_id)
        if run is None:
            raise HTTPException(409, "接入运行记录不完整")
        result = await db.execute(
            select(ExtractedFieldModel).where(ExtractedFieldModel.parse_run_id == run.parse_run_id)
        )
        view = build_fact_view(run, result.scalars().all())
        return {
            "event_id": event_id,
            "parse_run_id": str(run.parse_run_id),
            "document_id": str(run.document_id),
            "request_state": request.state,
            "status": run.status,
            "error_message": run.error_message,
            "facts": view.model_dump(mode="json"),
            "coverage": (
                (run.extraction_profile or {}).get("agent_runtime", {}).get("coverage")
                if isinstance((run.extraction_profile or {}).get("agent_runtime"), dict)
                else None
            ),
        }

    async def enqueue_result(self, db, run):
        """Called after extraction commit; only records local data, never sends HTTP."""
        if not self.config.enabled:
            return None
        profile = run.extraction_profile or {}
        incoming_id = profile.get("integration_event_id")
        destination = profile.get("integration_destination")
        if not incoming_id:
            return None
        request = await db.get(IntegrationRequest, incoming_id)
        if request is None:
            return None
        if run.status not in {"COMPLETED", "FAILED"}:
            return None
        request.state = "COMPLETED" if run.status == "COMPLETED" else "FAILED"
        if not destination:
            return None
        event_id = f"parse-result-{run.parse_run_id}"
        await _event_lock(db, event_id)
        existing = await db.get(IntegrationOutbox, event_id)
        if existing:
            return existing
        payload = await self.result(db, incoming_id)
        payload["delivery_event_id"] = event_id
        outbox = IntegrationOutbox(
            event_id=event_id,
            parse_run_id=run.parse_run_id,
            document_id=run.document_id,
            payload=payload,
            destination=destination,
            state="PENDING",
            attempts=0,
            max_attempts=5,
            next_attempt_at=utcnow(),
        )
        db.add(outbox)
        await db.flush()
        return outbox

    async def retry_delivery(self, db, event_id):
        result = await db.execute(
            select(IntegrationOutbox)
            .where(IntegrationOutbox.event_id == event_id)
            .with_for_update()
        )
        outbox = result.scalar_one_or_none()
        if outbox is None:
            raise HTTPException(404, "Outbox 事件不存在")
        if outbox.state != "FAILED":
            raise HTTPException(409, "只可恢复已耗尽重试的事件")
        outbox.state = "RETRY"
        outbox.max_attempts += 5
        outbox.next_attempt_at = utcnow()
        await db.flush()
        return outbox

    async def dispatch(self, db, event_id, *, transport=None, now: datetime | None = None):
        """Explicit operator/worker action. Delivery stays at-least-once with receiver idempotency."""
        config = self.config
        if not config.enabled or not config.webhook_enabled:
            raise HTTPException(503, "Webhook 分发未启用，未发送任何请求")
        result = await db.execute(
            select(IntegrationOutbox)
            .where(IntegrationOutbox.event_id == event_id)
            .with_for_update()
        )
        outbox = result.scalar_one_or_none()
        if outbox is None:
            raise HTTPException(404, "Outbox 事件不存在")
        if outbox.state == "DELIVERED":
            return outbox
        if outbox.state == "FAILED" or outbox.attempts >= outbox.max_attempts:
            raise HTTPException(409, "事件重试已耗尽，请显式恢复后重试")
        now = now or utcnow()
        if outbox.next_attempt_at and outbox.next_attempt_at > now:
            raise HTTPException(409, "尚未到达下一次重试时间")
        endpoint = config.destinations.get(outbox.destination)
        if not endpoint or not _endpoint_valid(endpoint):
            raise HTTPException(422, "目的地配置无效或未列入 HTTPS 允许列表")
        body = json.dumps(
            outbox.payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Idempotency-Key": outbox.event_id,
            "X-Platform-Event-ID": outbox.event_id,
        }
        if config.signing_secret:
            headers["X-Platform-Signature"] = (
                "sha256="
                + hmac.new(config.signing_secret.encode(), body, hashlib.sha256).hexdigest()
            )
        outbox.attempts += 1
        try:
            if transport is not None:
                response = await transport(endpoint, body, headers)
            else:
                async with httpx.AsyncClient(
                    timeout=15, verify=True, trust_env=False, follow_redirects=False
                ) as client:
                    response = await client.post(endpoint, content=body, headers=headers)
            acknowledgement = response.json() if 200 <= response.status_code < 300 else None
            valid_ack = (
                isinstance(acknowledgement, dict)
                and acknowledgement.get("event_id") == outbox.event_id
                and acknowledgement.get("acknowledged") is True
            )
            if not valid_ack:
                raise ValueError("missing_event_acknowledgement")
            outbox.state = "DELIVERED"
            outbox.acknowledgement = {
                "status_code": response.status_code,
                "received_at": now.isoformat(),
                "attempt": outbox.attempts,
                "receipt": acknowledgement,
            }
            outbox.last_error = None
            outbox.next_attempt_at = None
        except (httpx.HTTPError, ValueError, TypeError, TimeoutError, ConnectionError):
            outbox.last_error = "发送失败或未收到匹配 event_id 的确认回执"
            outbox.state = "FAILED" if outbox.attempts >= outbox.max_attempts else "RETRY"
            outbox.next_attempt_at = (
                None
                if outbox.state == "FAILED"
                else now + timedelta(seconds=min(30 * 2 ** min(outbox.attempts - 1, 6), 1800))
            )
        await db.flush()
        return outbox


integration_service = IntegrationService()


async def record_output_event(db, run):
    """Compatibility hook for ParseService: caller owns commit/rollback of the outbox."""
    return await integration_service.enqueue_result(db, run)
