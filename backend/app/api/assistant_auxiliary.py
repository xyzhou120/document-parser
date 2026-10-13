"""独立的助手卡片、对比和离线反馈可选接口。"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.assistant_auxiliary import (
    AssistantCompareRequest,
    AssistantFeedbackRequest,
    DocumentCardRequest,
)
from app.services.assistant.card_service import CardUnavailable, card_service
from app.services.assistant.compare_service import compare_service
from app.storage import get_db

router = APIRouter(prefix="/api/v1", tags=["文档助手辅助"])


def _error(exc):
    return HTTPException(status_code=404 if isinstance(exc, KeyError) else 409, detail=str(exc))


@router.get("/documents/{document_id}/assistant/card")
async def get_card(document_id: UUID, revision_id: UUID | None = None, generation_id: UUID | None = None,
                   db: AsyncSession = Depends(get_db)):
    try:
        return await card_service.get(db, document_id, revision_id=revision_id, generation_id=generation_id)
    except (KeyError, CardUnavailable, ValueError) as exc:
        raise _error(exc) from exc


@router.post("/documents/{document_id}/assistant/card", status_code=status.HTTP_202_ACCEPTED)
async def request_card(document_id: UUID, payload: DocumentCardRequest, db: AsyncSession = Depends(get_db)):
    try:
        return await card_service.request(db, document_id, **payload.model_dump())
    except (KeyError, CardUnavailable, ValueError) as exc:
        raise _error(exc) from exc


@router.post("/assistant/messages/{message_id}/compare")
async def compare_answer(message_id: UUID, payload: AssistantCompareRequest, db: AsyncSession = Depends(get_db)):
    try:
        return await compare_service.compare(db, message_id, **payload.model_dump())
    except (KeyError, ValueError) as exc:
        raise _error(exc) from exc


@router.post("/assistant/messages/{message_id}/feedback", status_code=status.HTTP_201_CREATED)
async def submit_feedback(message_id: UUID, payload: AssistantFeedbackRequest, db: AsyncSession = Depends(get_db)):
    # 数据模式部署后再导入；反馈不能藏在问答消息载荷中。
    from app.services.assistant.feedback_service import feedback_service
    try:
        return await feedback_service.submit(db, message_id, payload)
    except (KeyError, ValueError) as exc:
        raise _error(exc) from exc
