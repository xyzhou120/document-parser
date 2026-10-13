"""读取产物沿袭关系并追加来源修正；原始产物保持不可变。"""

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import DocumentArtifact
from app.models.source_revisions import SourceRevision
from app.schemas.source_revisions import (
    SourceExtractionPayload,
    SourceImpactResponse,
    SourceRevisionPayload,
    SourceRevisionResponse,
)
from app.services.source_revision_service import source_revision_service
from app.storage import get_db

router = APIRouter(prefix="/api/v1/artifacts", tags=["源文订正与产物"])


@router.post("/source-revisions/preview", summary="保存前预览源文订正与全量重算影响")
async def preview_revision(payload: SourceRevisionPayload, db: AsyncSession = Depends(get_db)):
    return await source_revision_service.preview(db, payload)


@router.post("/source-revisions", summary="追加源文区域确认/订正/不可读记录")
async def create_revision(payload: SourceRevisionPayload, db: AsyncSession = Depends(get_db)):
    revision, run = await source_revision_service.create(db, payload)
    return {"revision": SourceRevisionResponse.model_validate(revision),
            "run": {"parse_run_id": run.parse_run_id, "status": run.status,
                    "error_message": run.error_message, "mode": "FULL"} if run else None,
            "impact": await source_revision_service.impact(db, revision.revision_id)}


@router.get("/documents/{document_id}/history", summary="Artifact 历史及派生关系")
async def artifact_history(document_id: UUID, limit: int = Query(100, ge=1, le=500),
                           db: AsyncSession = Depends(get_db)):
    await source_revision_service.document(db, document_id)
    artifacts = (await db.execute(select(DocumentArtifact).where(
        DocumentArtifact.document_id == document_id).order_by(DocumentArtifact.created_at.desc()).limit(limit))).scalars().all()
    revisions = (await db.execute(select(SourceRevision).where(
        SourceRevision.document_id == document_id).order_by(SourceRevision.created_at.desc()))).scalars().all()
    return {"artifacts": [{"artifact_id": artifact.artifact_id, "document_id": document_id,
                           "file_hash": artifact.file_hash, "status": artifact.status,
                           "source_review_status": (artifact.quality or {}).get("source_review_status", "UNVERIFIED"),
                           "parent_artifact_id": (artifact.quality or {}).get("parent_artifact_id"),
                           "source_revision_id": (artifact.quality or {}).get("source_revision_id"),
                           "created_at": artifact.created_at} for artifact in artifacts],
            "revisions": [SourceRevisionResponse.model_validate(row) for row in revisions],
            "latest_revision_id": revisions[0].revision_id if revisions else None}


@router.get("/{artifact_id}/pages/{page_number}", summary="查看源文页索引与精确 offset")
async def artifact_page(artifact_id: UUID, page_number: int, db: AsyncSession = Depends(get_db)):
    from fastapi import HTTPException
    artifact = await source_revision_service.artifact(db, artifact_id)
    page = next((page for page in artifact.pages if page.get("page_number") == page_number), None)
    if page is None:
        raise HTTPException(404, "Artifact 页面不存在")
    return {"artifact_id": artifact_id, "file_hash": artifact.file_hash, "page": page,
            "source_review_status": (artifact.quality or {}).get("source_review_status", "UNVERIFIED")}


@router.get("/source-revisions/{revision_id}/impact", response_model=SourceImpactResponse,
            summary="订正影响预览（保守全量重算）")
async def impact(revision_id: UUID, db: AsyncSession = Depends(get_db)):
    return await source_revision_service.impact(db, revision_id)


@router.post("/source-revisions/{revision_id}/retry", summary="显式重试队列提交失败的源文重算")
async def retry(revision_id: UUID, db: AsyncSession = Depends(get_db)):
    run = await source_revision_service.retry(db, revision_id)
    return {"parse_run_id": run.parse_run_id, "status": run.status, "error_message": run.error_message}


@router.post("/source-revisions/{revision_id}/extract", summary="显式固定派生 Artifact 全量重算")
async def extract(revision_id: UUID, payload: SourceExtractionPayload, db: AsyncSession = Depends(get_db)):
    run = await source_revision_service.extract(db, revision_id, payload)
    return {"parse_run_id": run.parse_run_id, "document_id": run.document_id,
            "status": run.status, "error_message": run.error_message,
            "mode": "FULL", "artifact_id": run.artifact_id}
