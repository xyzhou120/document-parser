"""文档工作区：显式上下文、关联关系与提取影响预览。"""

import copy
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import Document, DocumentArtifact, ExtractedFieldModel, ParseRun
from app.models.document_context import (
    DocumentContext,
    DocumentContextAudit,
    DocumentRelation,
    DocumentRelationAudit,
)
from app.rules.repository import rule_repository
from app.services.artifact_service import preprocessing_profile
from app.services.document_service import plan_rule_impact, related_fact_candidates
from app.services.execution.job_repository import write_extracted_field_counts
from app.services.parse_service import rule_snapshot
from app.storage import get_db

router = APIRouter(prefix="/api/v1/documents", tags=["文档工作区"])
SUBTYPES = {"report", "report_sheet", "statement", "design", "ledger", "approval", "unknown"}


class DecisionInput(BaseModel):
    actor: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=2000)
    expected_revision: int = Field(default=0, ge=0)

    @field_validator("actor", "reason")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("操作人与原因不能为空")
        return value.strip()


class ContextInput(DecisionInput):
    subtype: str = "unknown"
    context: dict = Field(default_factory=dict)

    @field_validator("subtype")
    @classmethod
    def valid_subtype(cls, value):
        if value not in SUBTYPES:
            raise ValueError("附件子类型不受支持")
        return value


class RelationInput(DecisionInput):
    target_document_id: UUID
    source_role: str = Field(min_length=1, max_length=30)
    relation_type: str = Field(default="attachment", min_length=1, max_length=40)

    @field_validator("source_role", "relation_type")
    @classmethod
    def nonblank_relation(cls, value):
        if not value.strip() or value.strip().lower() in {"unknown", "未知"}:
            raise ValueError("请明确填写来源角色和关系类型")
        return value.strip()


def context_dict(row):
    return {
        "context_id": str(row.context_id),
        "document_id": str(row.document_id),
        "subtype": row.subtype,
        "context": row.context,
        "revision": row.revision,
        "actor": row.actor,
        "reason": row.reason,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def relation_dict(row):
    return {
        "relation_id": str(row.relation_id),
        "source_document_id": str(row.source_document_id),
        "target_document_id": str(row.target_document_id),
        "source_role": row.source_role,
        "relation_type": row.relation_type,
        "actor": row.actor,
        "reason": row.reason,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


async def require_document(db, document_id, *, lock=False):
    document = (
        (
            await db.scalar(
                select(Document).where(Document.document_id == document_id).with_for_update()
            )
        )
        if lock
        else await db.get(Document, document_id)
    )
    if not document:
        raise HTTPException(404, "文档不存在")
    return document


async def latest_completed(db, document_id):
    return await db.scalar(
        select(ParseRun)
        .where(ParseRun.document_id == document_id, ParseRun.status == "COMPLETED")
        .order_by(ParseRun.created_at.desc(), ParseRun.parse_run_id.desc())
        .limit(1)
    )


async def relation_revision(db, document_id):
    return (
        await db.scalar(
            select(func.max(DocumentRelationAudit.revision)).where(
                DocumentRelationAudit.source_document_id == document_id
            )
        )
        or 0
    )


@router.get("/{document_id}/runs")
async def document_runs(document_id: UUID, db: AsyncSession = Depends(get_db)):
    document = await require_document(db, document_id)
    rows = (
        (
            await db.execute(
                select(ParseRun)
                .where(ParseRun.document_id == document_id)
                .order_by(ParseRun.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    await write_extracted_field_counts(db, rows, repair_zeros=True)
    keys = (
        "parse_run_id",
        "parent_parse_run_id",
        "status",
        "run_mode",
        "artifact_id",
        "requested_field_keys",
        "fields_extracted",
        "mean_confidence",
        "quality_status",
        "error_message",
        "created_at",
        "started_at",
        "completed_at",
    )
    return {
        "document_id": document_id,
        "file_name": document.file_name,
        "mine_name": document.mine_name,
        "items": [{key: getattr(row, key, None) for key in keys} for row in rows],
    }


@router.get("/{document_id}/context")
async def get_context(document_id: UUID, db: AsyncSession = Depends(get_db)):
    await require_document(db, document_id)
    rows = (
        (
            await db.execute(
                select(DocumentContext)
                .where(DocumentContext.document_id == document_id)
                .order_by(DocumentContext.revision.desc())
            )
        )
        .scalars()
        .all()
    )
    return {
        "document_id": document_id,
        "current": context_dict(rows[0]) if rows else None,
        "history": [context_dict(row) for row in rows],
        "effect": "Human context annotation only; existing facts and parse snapshots remain unchanged.",
    }


@router.post("/{document_id}/context")
async def set_context(document_id: UUID, payload: ContextInput, db: AsyncSession = Depends(get_db)):
    # 即使首次修订还没有上下文记录，也要锁定父记录。
    await require_document(db, document_id, lock=True)
    current = await db.scalar(
        select(DocumentContext)
        .where(DocumentContext.document_id == document_id)
        .order_by(DocumentContext.revision.desc())
        .limit(1)
    )
    actual = current.revision if current else 0
    if payload.expected_revision != actual:
        raise HTTPException(409, f"上下文已更新，请刷新后重试（当前版本{actual}）")
    row = DocumentContext(
        document_id=document_id,
        subtype=payload.subtype,
        context=copy.deepcopy(payload.context),
        revision=actual + 1,
        actor=payload.actor,
        reason=payload.reason,
    )
    db.add(row)
    await db.flush()
    db.add(
        DocumentContextAudit(
            document_id=document_id,
            context_id=row.context_id,
            action="CONFIRM",
            before=context_dict(current) if current else None,
            after=context_dict(row),
            actor=payload.actor,
            reason=payload.reason,
        )
    )
    await db.commit()
    return context_dict(row)


@router.get("/{document_id}/impact-plan")
async def impact_plan(document_id: UUID, db: AsyncSession = Depends(get_db)):
    document = await require_document(db, document_id)
    parent = await latest_completed(db, document_id)
    previous = (parent.extraction_profile or {}) if parent else {}
    engine = previous.get("extraction_engine", "checklist_agent")
    rules = await rule_repository.get_rules_for_document(db, document)
    current = rule_snapshot(rules, document.checklist_type, engine)
    artifact = (
        await db.get(DocumentArtifact, parent.artifact_id)
        if parent and parent.artifact_id
        else None
    )
    version = preprocessing_profile(engine)["version"]
    compatible = bool(
        artifact
        and artifact.status == "READY"
        and artifact.document_id == document_id
        and artifact.file_hash == document.file_hash
        and artifact.preprocess_profile_version == version
    )
    reasons = (
        []
        if compatible
        else (["NO_COMPLETED_PARENT"] if not parent else ["ARTIFACT_MISSING_OR_PROFILE_MISMATCH"])
    )
    if not parent or "rules" not in previous:
        compatible = False
        reasons.append("MISSING_PARENT_RULE_SNAPSHOT")
    plan = plan_rule_impact(previous, current, artifact_compatible=compatible, full_reasons=reasons)
    return {
        "document_id": document_id,
        "parent_parse_run_id": parent.parse_run_id if parent else None,
        "checklist_type": document.checklist_type,
        "current_rule_count": len(rules),
        "artifact_profile_version": version,
        **plan,
    }


@router.get("/{document_id}/relations")
async def list_relations(document_id: UUID, db: AsyncSession = Depends(get_db)):
    await require_document(db, document_id)
    rows = (
        (
            await db.execute(
                select(DocumentRelation)
                .where(
                    (DocumentRelation.source_document_id == document_id)
                    | (DocumentRelation.target_document_id == document_id)
                )
                .order_by(DocumentRelation.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    audit = (
        (
            await db.execute(
                select(DocumentRelationAudit)
                .where(DocumentRelationAudit.source_document_id == document_id)
                .order_by(DocumentRelationAudit.revision.desc())
            )
        )
        .scalars()
        .all()
    )
    return {
        "document_id": document_id,
        "revision": audit[0].revision if audit else 0,
        "items": [relation_dict(row) for row in rows],
        "history": [
            {
                "revision": row.revision,
                "action": row.action,
                "actor": row.actor,
                "reason": row.reason,
                "snapshot": row.snapshot,
                "created_at": row.created_at,
            }
            for row in audit
        ],
    }


@router.post("/{document_id}/relations")
async def link_document(
    document_id: UUID, payload: RelationInput, db: AsyncSession = Depends(get_db)
):
    # 按固定顺序加锁，避免双向关联产生死锁。
    documents = {}
    for current_id in sorted({document_id, payload.target_document_id}, key=str):
        documents[current_id] = await require_document(db, current_id, lock=True)
    source, target = documents[document_id], documents[payload.target_document_id]
    if source.document_id == target.document_id:
        raise HTTPException(422, "文档不能关联自身")
    if not source.mine_id or not target.mine_id or source.mine_id != target.mine_id:
        raise HTTPException(422, "仅允许关联已确认属于同一煤矿的文档")
    actual = await relation_revision(db, document_id)
    if payload.expected_revision != actual:
        raise HTTPException(409, "关系版本已更新，请刷新后重试")
    existing = await db.scalar(
        select(DocumentRelation).where(
            (
                (DocumentRelation.source_document_id == document_id)
                & (DocumentRelation.target_document_id == payload.target_document_id)
            )
            | (
                (DocumentRelation.source_document_id == payload.target_document_id)
                & (DocumentRelation.target_document_id == document_id)
            )
        )
    )
    if existing:
        raise HTTPException(409, "文档关系已存在")
    row = DocumentRelation(
        source_document_id=document_id,
        target_document_id=target.document_id,
        source_role=payload.source_role,
        relation_type=payload.relation_type,
        actor=payload.actor,
        reason=payload.reason,
    )
    db.add(row)
    await db.flush()
    db.add(
        DocumentRelationAudit(
            relation_id=row.relation_id,
            source_document_id=document_id,
            target_document_id=target.document_id,
            action="LINK",
            revision=actual + 1,
            snapshot=relation_dict(row),
            actor=payload.actor,
            reason=payload.reason,
        )
    )
    await db.commit()
    return {"relation": relation_dict(row), "revision": actual + 1}


@router.delete("/{document_id}/relations/{relation_id}")
async def unlink_document(
    document_id: UUID, relation_id: UUID, payload: DecisionInput, db: AsyncSession = Depends(get_db)
):
    await require_document(db, document_id, lock=True)
    actual = await relation_revision(db, document_id)
    if payload.expected_revision != actual:
        raise HTTPException(409, "关系版本已更新，请刷新后重试")
    row = await db.scalar(
        select(DocumentRelation).where(
            DocumentRelation.relation_id == relation_id,
            DocumentRelation.source_document_id == document_id,
        )
    )
    if not row:
        raise HTTPException(404, "文档关系不存在；请从关系创建方解除关联")
    snapshot = relation_dict(row)
    db.add(
        DocumentRelationAudit(
            relation_id=row.relation_id,
            source_document_id=row.source_document_id,
            target_document_id=row.target_document_id,
            action="UNLINK",
            revision=actual + 1,
            snapshot=snapshot,
            actor=payload.actor,
            reason=payload.reason,
        )
    )
    await db.delete(row)
    await db.commit()
    return {"deleted": True, "relation": snapshot, "revision": actual + 1}


@router.get("/{document_id}/related-facts")
async def related_facts(document_id: UUID, db: AsyncSession = Depends(get_db)):
    document = await require_document(db, document_id)
    relations = (
        (
            await db.execute(
                select(DocumentRelation).where(
                    (DocumentRelation.source_document_id == document_id)
                    | (DocumentRelation.target_document_id == document_id)
                )
            )
        )
        .scalars()
        .all()
    )
    roles = {document_id: "current_document"}
    links = {}
    for relation in relations:
        other = (
            relation.target_document_id
            if relation.source_document_id == document_id
            else relation.source_document_id
        )
        roles[other] = (
            relation.source_role if relation.source_document_id == document_id else "relation_owner"
        )
        links[other] = relation_dict(relation)
    from app.services.fact_service import build_fact_view

    sources = []
    for current_id in sorted(roles, key=str):
        other = document if current_id == document_id else await db.get(Document, current_id)
        # 防御性检查：元数据变更不能悄然导致跨矿数据暴露。
        if not other or (
            current_id != document_id
            and (not document.mine_id or other.mine_id != document.mine_id)
        ):
            continue
        run = await latest_completed(db, current_id)
        if not run:
            sources.append(
                {
                    "document_id": str(current_id),
                    "file_name": other.file_name,
                    "checklist_type": other.checklist_type,
                    "parse_run_id": None,
                    "source_role": roles[current_id],
                    "facts": [],
                    "status": "NO_COMPLETED_RUN",
                }
            )
            continue
        fields = (
            (
                await db.execute(
                    select(ExtractedFieldModel).where(
                        ExtractedFieldModel.parse_run_id == run.parse_run_id
                    )
                )
            )
            .scalars()
            .all()
        )
        view = build_fact_view(run, fields)
        sources.append(
            {
                "document_id": str(current_id),
                "file_name": other.file_name,
                "checklist_type": other.checklist_type,
                "parse_run_id": str(run.parse_run_id),
                "source_role": roles[current_id],
                "relation": links.get(current_id),
                "facts": [fact.model_dump(mode="json") for fact in view.facts],
            }
        )
    return {"document_id": document_id, "sources": sources, **related_fact_candidates(sources)}
