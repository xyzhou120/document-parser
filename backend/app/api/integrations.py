"""入站协议适配与显式启用的结果投递。"""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import ParseRun
from app.models.integrations import IntegrationOutbox, IntegrationRequest
from app.security import assert_actor, require_scope
from app.services.integration_service import integration_service
from app.storage import get_db
from app.tasks import parse_document_task

router = APIRouter(prefix="/api/v1/integrations", tags=["外部接入"])


class IntegrationParseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: str = Field(
        min_length=1, max_length=160, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$"
    )
    document_id: UUID
    engine: Literal["rules", "checklist_agent"]
    rule_bundle_id: UUID | None = None
    force_refresh: bool = False
    destination: str | None = Field(default=None, max_length=100)


async def _submit(db, record, force_refresh=False):
    # 提交队列前，工作进程必须能够读取不可变快照。
    await db.commit()
    try:
        task = parse_document_task.delay(
            str(record.document_id), str(record.parse_run_id), None, force_refresh
        )
    except Exception as exc:
        record.state = "QUEUE_FAILED"
        run = await db.get(ParseRun, record.parse_run_id)
        if run:
            run.status = "FAILED"
            run.error_message = "外部事件解析任务提交失败，可通过事件重试恢复"
        await db.commit()
        raise HTTPException(503, "解析任务提交失败，接入记录已保留，可按事件重试") from exc
    return task


@router.post("/parse", summary="接收已上传文档的外部解析事件")
async def accept_parse_event(
    payload: IntegrationParseRequest, request: Request, db: AsyncSession = Depends(get_db)
):
    principal = require_scope(request, "integration")
    assert_actor(principal, request.headers.get("x-actor"))
    record, created = await integration_service.accept(
        db, payload.model_dump(mode="json"), principal.actor
    )
    if created:
        task = await _submit(db, record, payload.force_refresh)
        return {
            "event_id": record.event_id,
            "parse_run_id": record.parse_run_id,
            "document_id": record.document_id,
            "status": record.state,
            "celery_task_id": task.id,
            "idempotent_replay": False,
        }
    return {
        "event_id": record.event_id,
        "parse_run_id": record.parse_run_id,
        "document_id": record.document_id,
        "status": record.state,
        "idempotent_replay": True,
    }


@router.post("/events/{event_id}/retry", summary="重试队列提交失败的接入事件")
async def retry_parse_event(event_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    require_scope(request, "integration")
    from app.services.integration_service import _event_lock

    await _event_lock(db, event_id)
    record = await db.get(IntegrationRequest, event_id)
    if record is None:
        raise HTTPException(404, "接入事件不存在")
    if record.state != "QUEUE_FAILED":
        raise HTTPException(409, "仅队列提交失败事件可在此重试；解析失败请创建新的 event_id")
    run = await db.get(ParseRun, record.parse_run_id)
    if run is None:
        raise HTTPException(409, "接入运行记录不完整")
    record.state = "PENDING"
    run.status = "PENDING"
    run.error_message = None
    task = await _submit(db, record, bool(record.payload.get("force_refresh")))
    return {
        "event_id": event_id,
        "parse_run_id": str(record.parse_run_id),
        "state": record.state,
        "celery_task_id": task.id,
    }


@router.get("/events/{event_id}/result", summary="查询外部事件的事实结果")
async def integration_result(event_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    require_scope(request, "integration")
    return await integration_service.result(db, event_id)


@router.post("/outbox/{event_id}/dispatch", summary="显式发送一个 Webhook outbox 事件")
async def dispatch_outbox(event_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    require_scope(request, "integration")
    result = await integration_service.dispatch(db, event_id)
    return {
        "event_id": result.event_id,
        "state": result.state,
        "attempts": result.attempts,
        "next_attempt_at": result.next_attempt_at,
        "acknowledgement": result.acknowledgement,
    }


@router.get("/outbox", summary="查询持久化外发事件")
async def list_outbox(request: Request, db: AsyncSession = Depends(get_db)):
    require_scope(request, "integration")
    result = await db.execute(
        select(IntegrationOutbox).order_by(IntegrationOutbox.created_at.desc()).limit(100)
    )
    return [
        {
            "event_id": row.event_id,
            "parse_run_id": str(row.parse_run_id),
            "destination": row.destination,
            "state": row.state,
            "attempts": row.attempts,
            "max_attempts": row.max_attempts,
            "next_attempt_at": row.next_attempt_at,
            "last_error": row.last_error,
            "acknowledgement": row.acknowledgement,
        }
        for row in result.scalars().all()
    ]


@router.post("/outbox/{event_id}/retry", summary="显式恢复耗尽的外发事件（不立即发送）")
async def retry_outbox(event_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    require_scope(request, "integration")
    result = await integration_service.retry_delivery(db, event_id)
    return {
        "event_id": result.event_id,
        "state": result.state,
        "attempts": result.attempts,
        "max_attempts": result.max_attempts,
        "next_attempt_at": result.next_attempt_at,
    }
