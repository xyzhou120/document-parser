"""规则包试运行、发布、回滚与不可变审计接口。"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.releases import RuleBundle, RuleReleaseEvent
from app.schemas.releases import (
    BundleResponse,
    DraftBundlePayload,
    ReleaseCommand,
    ReleaseEventResponse,
)
from app.services.release_service import release_service, validate_bundle
from app.storage import get_db

router = APIRouter(prefix="/api/v1/rule-releases", tags=["规则发布"])


async def _response(db, bundle):
    return BundleResponse(**await release_service.describe(db, bundle))


@router.post("/drafts", response_model=BundleResponse, summary="创建不可变规则草稿")
async def create_draft(payload: DraftBundlePayload, db: AsyncSession = Depends(get_db)):
    bundle = await release_service.create_draft(db, payload)
    return await _response(db, bundle)


@router.get("/drafts/{bundle_id}", response_model=BundleResponse, summary="查看规则 bundle")
async def get_bundle(bundle_id: UUID, db: AsyncSession = Depends(get_db)):
    return await _response(db, await release_service.get(db, bundle_id))


@router.get("/{checklist_type}/active", response_model=BundleResponse, summary="查看当前发布规则")
async def active_bundle(checklist_type: str, db: AsyncSession = Depends(get_db)):
    head = await release_service.head(db, checklist_type)
    if head is None:
        raise HTTPException(404, "该清单尚无发布 bundle，当前使用编辑规则")
    return await _response(db, await release_service.get(db, head.bundle_id))


@router.post("/drafts/{bundle_id}/validate", summary="验证草稿配置")
async def validate_draft(bundle_id: UUID, db: AsyncSession = Depends(get_db)):
    bundle = await release_service.get(db, bundle_id)
    return {"bundle_id": str(bundle.bundle_id), **validate_bundle(bundle)}


@router.get("/drafts/{bundle_id}/preview", summary="配置 dry-run 与历史差异")
async def preview(bundle_id: UUID, baseline_id: UUID | None = Query(None),
                  db: AsyncSession = Depends(get_db)):
    return await release_service.preview(db, await release_service.get(db, bundle_id), baseline_id)


@router.post("/drafts/{bundle_id}/publish", response_model=ReleaseEventResponse,
             summary="显式发布规则 bundle")
async def publish(bundle_id: UUID, command: ReleaseCommand, db: AsyncSession = Depends(get_db)):
    event = await release_service.activate(db, bundle_id, command)
    return event


@router.post("/drafts/{bundle_id}/rollback", response_model=ReleaseEventResponse,
             summary="显式回滚到历史发布 bundle")
async def rollback(bundle_id: UUID, command: ReleaseCommand, db: AsyncSession = Depends(get_db)):
    event = await release_service.activate(db, bundle_id, command, rollback=True)
    return event


@router.get("/{checklist_type}/history", response_model=list[ReleaseEventResponse], summary="发布审计")
async def history(checklist_type: str, limit: int = Query(100, ge=1, le=500),
                  db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(RuleReleaseEvent).where(
        RuleReleaseEvent.checklist_type == checklist_type).order_by(
            RuleReleaseEvent.created_at.desc()).limit(limit))
    return list(result.scalars().all())


@router.get("/{checklist_type}/bundles", response_model=list[BundleResponse], summary="规则 bundle 列表")
async def bundles(checklist_type: str, limit: int = Query(100, ge=1, le=500),
                  db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(RuleBundle).where(
        RuleBundle.checklist_type == checklist_type).order_by(RuleBundle.version.desc()).limit(limit))
    return [await _response(db, bundle) for bundle in result.scalars().all()]
