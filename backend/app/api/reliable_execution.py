"""P13-A 可靠提取的执行状态与恢复接口。"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.schemas.execution import (
    ExecutionResponse,
    FieldJobsResponse,
    ReliableExtractRequest,
    RunControlResponse,
)
from app.services.reliable_extraction_service import (
    ContentPreparationRequired,
    ReliableExecutionError,
    reliable_extraction_service,
)
from app.storage import get_db

router = APIRouter(prefix="/api/v1/parse/runs", tags=["可靠提取执行"])
start_router = APIRouter(prefix="/api/v1/parse/documents", tags=["可靠提取执行"])


def _enabled() -> None:
    if not settings.app.reliable_enabled:
        raise HTTPException(status_code=404, detail="reliable extraction is disabled")


def _handle(exc: Exception) -> HTTPException:
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, ReliableExecutionError | ValueError):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=500, detail="reliable execution state unavailable")


@router.get("/{run_id}/execution", response_model=ExecutionResponse)
async def execution(run_id: UUID, limit: int = Query(100, ge=1, le=500), cursor: str | None = None,
                    db: AsyncSession = Depends(get_db)):
    _enabled()
    try:
        return await reliable_extraction_service.execution(db, run_id, limit=limit, cursor=cursor)
    except Exception as exc:
        raise _handle(exc) from exc


@router.get("/{run_id}/field-jobs", response_model=FieldJobsResponse)
async def field_jobs(run_id: UUID, limit: int = Query(100, ge=1, le=500), cursor: str | None = None,
                     db: AsyncSession = Depends(get_db)):
    _enabled()
    try:
        return await reliable_extraction_service.field_jobs(db, run_id, limit=limit, cursor=cursor)
    except Exception as exc:
        raise _handle(exc) from exc


async def _control(run_id: UUID, action: str, retry_failed: bool, db: AsyncSession,
                   document_token_hard_limit: int | None = None):
    _enabled()
    try:
        return await reliable_extraction_service.control(db, run_id, action, retry_failed=retry_failed,
            document_token_hard_limit=document_token_hard_limit)
    except Exception as exc:
        raise _handle(exc) from exc


@router.post("/{run_id}/resume", response_model=RunControlResponse)
async def resume(run_id: UUID, retry_failed: bool = False,
                 document_token_hard_limit: int | None = Query(None, ge=1, le=10_000_000_000),
                 db: AsyncSession = Depends(get_db)):
    return await _control(run_id, "resume", retry_failed, db, document_token_hard_limit)


@router.post("/{run_id}/pause", response_model=RunControlResponse)
async def pause(run_id: UUID, db: AsyncSession = Depends(get_db)):
    return await _control(run_id, "pause", False, db)


@router.post("/{run_id}/cancel", response_model=RunControlResponse)
async def cancel(run_id: UUID, db: AsyncSession = Depends(get_db)):
    return await _control(run_id, "cancel", False, db)


@start_router.post("/{document_id}/reliable-extract", status_code=202)
async def reliable_extract(document_id: UUID, payload: ReliableExtractRequest,
                           db: AsyncSession = Depends(get_db)):
    _enabled()
    from app.models.database import Document
    from app.rules.repository import rule_repository
    try:
        document = await db.get(Document, document_id)
        if document is None:
            raise KeyError(document_id)
        rules = await rule_repository.get_rules_for_document(db, document)
        if payload.field_keys is not None:
            selected = set(payload.field_keys)
            unknown = selected - {rule.field_key for rule in rules}
            if unknown:
                raise ValueError(f"unknown enabled fields: {sorted(unknown)}")
            rules = [rule for rule in rules if rule.field_key in selected]
        run = await reliable_extraction_service.start(db, document_id, rules=rules,
                revision_id=payload.revision_id, parent_run_id=payload.parent_run_id,
                document_token_hard_limit=payload.document_token_hard_limit, request_id=payload.request_id)
        # 初始发件箱是恢复依据；先提交，再发布消息，防止工作进程抢先读取未提交的账本记录。
        await db.commit()
        from app.tasks import reliable_execution_wakeup_task
        try:
            task = reliable_execution_wakeup_task.apply_async(args=[str(run.parse_run_id)],
                                                              queue="reliable-execution")
            task_id = task.id
        except Exception as exc:
            # 持久化的初始发件箱仍处于待处理状态；消息代理停机不能把尚未尝试的字段判为最终失败。
            run.error_message = f"dispatch pending recovery: {type(exc).__name__}"
            await db.commit()
            task_id = None
        return {"contract_version": "reliable-extraction-v2", "parse_run_id": run.parse_run_id,
                "document_id": document_id, "status": run.status,
                "content_revision_id": run.content_revision_id, "automatic_complete": False,
                "task_id": task_id}
    except Exception as exc:
        if isinstance(exc, ContentPreparationRequired):
            await db.commit()
            from app.tasks import reliable_content_build_task
            try:
                reliable_content_build_task.apply_async(args=[str(exc.build_id)], queue="reliable-execution")
            except Exception:
                pass  # 持久化的 content.build.execute 发件箱可补偿消息丢失。
            return {"contract_version": "reliable-extraction-v2", "document_id": document_id,
                    "parse_run_id": None, "build_id": exc.build_id, "status": "PREPARING_CONTENT",
                    "automatic_complete": False}
        raise _handle(exc) from exc


@start_router.get("/{document_id}/content-status")
async def content_status(document_id: UUID, build_id: UUID | None = None, db: AsyncSession = Depends(get_db)):
    _enabled()
    from sqlalchemy import select

    from app.models.content import ContentBuildJob
    from app.services.content_preparation_service import content_preparation_service
    query = select(ContentBuildJob).where(ContentBuildJob.document_id == document_id)
    if build_id is not None:
        query = query.where(ContentBuildJob.build_id == build_id)
    build = await db.scalar(query.order_by(ContentBuildJob.created_at.desc()).limit(1))
    if build is None:
        raise HTTPException(404, "content build不存在")
    state = content_preparation_service.serialize(build)
    return {**state, "parse_run_id": (build.checkpoint or {}).get("parse_run_id"),
            "content_revision_id": (build.checkpoint or {}).get("published_revision_id"),
            "last_wait_reason": (build.checkpoint or {}).get("last_wait_reason"),
            "status": "PREPARING_CONTENT" if build.phase != "published" else "CONTENT_READY"}


@start_router.post("/{document_id}/content-build/{build_id}/resume", status_code=202)
async def resume_content_build(document_id: UUID, build_id: UUID, db: AsyncSession = Depends(get_db)):
    _enabled()
    from app.models.content import ContentBuildJob
    from app.models.database import Document
    build = await db.get(ContentBuildJob, build_id, with_for_update=True, populate_existing=True)
    if build is None or build.document_id != document_id:
        raise HTTPException(404, "content build不存在")
    if build.phase == "published":
        return {"build_id": build_id, "status": "CONTENT_READY",
                "parse_run_id": (build.checkpoint or {}).get("parse_run_id")}
    document = await db.get(Document, document_id)
    if document is None or document.file_hash != build.file_hash:
        raise HTTPException(409, "原文件版本已变化，请重新提交内容准备")
    if build.dispatch_state in {"cancelled", "superseded", "failed_terminal"}:
        raise HTTPException(409, "该内容构建状态不可原地恢复，请创建新的内容准备请求")
    if "circuit_exhausted" in str(build.last_error or ""):
        raise HTTPException(409, "OCR服务熔断已耗尽恢复次数，请管理员单独恢复服务后重新提交")
    from copy import deepcopy
    from datetime import datetime

    from app.services.execution import JobRepository
    if build.dispatch_state == "running" and build.lease_until and build.lease_until > datetime.utcnow():
        raise HTTPException(409, "content build仍在执行")
    build.fence_token += 1
    build.lease_owner = None
    build.lease_until = None
    checkpoint = deepcopy(build.checkpoint or {})
    for region in checkpoint.get("regions", {}).values():
        if region.get("state") not in {"processed", "blank"}:
            region["previous_failure"] = {key: region.get(key) for key in ("state", "error", "attempts")}
            region.update(state="pending", attempts=0)
    checkpoint.pop("retry_at", None)
    build.checkpoint = checkpoint
    build.dispatch_state = "queued"
    build.last_error = None
    await JobRepository(db).enqueue_outbox(kind="content.build.execute", aggregate_id=build_id,
        dedupe_key=f"content-build:{build_id}:resume:{build.fence_token}",
        payload={"build_id": str(build_id), "document_id": str(document_id), "file_hash": build.file_hash})
    await db.commit()
    from app.tasks import reliable_content_build_task
    task_id = None
    try:
        task = reliable_content_build_task.apply_async(args=[str(build_id)], queue="reliable-execution")
        task_id = task.id
    except Exception:
        # 持久化的 content.build.execute 事件负责恢复。
        pass
    return {"build_id": build_id, "status": "PREPARING_CONTENT", "task_id": task_id,
            "parse_run_id": (build.checkpoint or {}).get("parse_run_id")}
