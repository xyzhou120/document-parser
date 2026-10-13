"""Document assistant HTTP boundary; execution is always queued/worker-owned."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.assistant import (
    AssistantLocationResponse,
    AssistantMessageCreate,
    AssistantMessageResponse,
    AssistantSessionCreate,
    AssistantSessionResponse,
    AssistantStatusResponse,
)
from app.services.assistant.qa_service import AssistantUnavailable, QAService, default_qa_service
from app.services.assistant.retrieval_service import IndexUnavailable
from app.storage import get_db

router = APIRouter(prefix="/api/v1", tags=["文档助手"])


def _service(db: AsyncSession) -> QAService:
    return default_qa_service()


def _message_response(row, *, locations=None):
    locations = locations or {}
    return {"message_id": row["message_id"], "session_id": row["session_id"],
            "conversation_id": row["session_id"], "client_message_id": row["client_message_id"],
            "question": row["question"], "mode": row["mode"], "revision_id": row["revision_id"],
            "generation_id": row["generation_id"], "execution_status": row["execution_state"],
            "answer": row.get("answer"), "claims": row.get("claims") or [],
            "citations": row.get("citations") or [], "risks": (row.get("checkpoint") or {}).get("risks", []),
            "error": row.get("error"), "scope_snapshot": row.get("scope_snapshot") or {},
            "next_cursor": locations.get("next_cursor"),
            "search_finished": locations.get("search_finished", bool((row.get("checkpoint") or {}).get("search_finished"))),
            "remaining_work": locations.get("remaining_work", row["execution_state"] in {"queued", "running", "waiting_service"})}


@router.get("/documents/{document_id}/assistant/status", response_model=AssistantStatusResponse)
async def assistant_status(document_id: UUID, db: AsyncSession = Depends(get_db)):
    return await _service(db).status(document_id)


@router.post("/documents/{document_id}/assistant/sessions", response_model=AssistantSessionResponse,
             status_code=status.HTTP_201_CREATED)
async def create_assistant_session(document_id: UUID, payload: AssistantSessionCreate,
                                   db: AsyncSession = Depends(get_db)):
    try:
        service = _service(db)
        row = await service.create_session(document_id=document_id, request_id=payload.request_id,
                                           revision_id=payload.revision_id, generation_id=payload.generation_id)
    except (AssistantUnavailable, ValueError) as exc:
        raise HTTPException(status_code=409, detail={"code": "assistant_unavailable", "reason": str(exc)}) from exc
    return {"session_id": row["session_id"], "conversation_id": row["session_id"],
            "document_id": row["document_id"], "revision_id": row["active_revision_id"],
            "generation_id": row["active_generation_id"], "status": row["status"]}


@router.post("/assistant/sessions/{session_id}/messages", response_model=AssistantMessageResponse,
             status_code=status.HTTP_202_ACCEPTED)
async def create_assistant_message(session_id: UUID, payload: AssistantMessageCreate,
                                   db: AsyncSession = Depends(get_db)):
    try:
        row, _ = await _service(db).submit_message(
            session_id, request_id=payload.request_id, question=payload.question,
            mode=payload.mode, parent_message_id=payload.parent_message_id)
    except (AssistantUnavailable, ValueError) as exc:
        raise HTTPException(status_code=409, detail={"code": "message_rejected", "reason": str(exc)}) from exc
    return _message_response(row)


@router.get("/assistant/sessions/{session_id}/messages")
async def list_assistant_messages(session_id: UUID, cursor: str | None = Query(None),
                                  limit: int = Query(50, ge=1, le=100), db: AsyncSession = Depends(get_db)):
    rows, next_cursor = await _service(db).repository.list_messages(session_id, cursor=cursor, limit=limit)
    return {"items": [_message_response(row) for row in rows], "next_cursor": next_cursor}


@router.get("/assistant/messages/{message_id}", response_model=AssistantMessageResponse)
async def get_assistant_message(message_id: UUID, db: AsyncSession = Depends(get_db)):
    try:
        row = await _service(db).get_message(message_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="消息不存在") from exc
    return _message_response(row)


@router.get("/assistant/messages/{message_id}/locations", response_model=AssistantLocationResponse)
async def assistant_locations(message_id: UUID, cursor: str | None = Query(None),
                              limit: int = Query(20, ge=1, le=100), db: AsyncSession = Depends(get_db)):
    try:
        result = await _service(db).get_locations(message_id, cursor=cursor, limit=limit)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=404 if isinstance(exc, KeyError) else 422,
                            detail=str(exc)) from exc
    return result


@router.post("/assistant/messages/{message_id}/resume", response_model=AssistantMessageResponse,
             status_code=status.HTTP_202_ACCEPTED)
async def resume_assistant_message(message_id: UUID, db: AsyncSession = Depends(get_db)):
    try:
        row = await _service(db).resume_message(message_id)
    except (KeyError, AssistantUnavailable, IndexUnavailable) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _message_response(row)
