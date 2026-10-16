"""可选的人工复核工作队列。

此投影有意保持只读：汇总提取记录和最新的只追加复核审计，
不修改来源字段，也不为下游设置审批门槛。
"""

from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models.database import Document, ExtractedFieldModel, ParseRun
from app.models.reviews import FieldReviewRevision
from app.services.fact_service import build_fact_view
from app.storage import get_db

router = APIRouter(prefix="/api/v1/review-workspace", tags=["人工核对工作台"])


def _risk_codes(field: ExtractedFieldModel) -> list[str]:
    detail = field.extraction_detail or {}
    issues: list[str] = []
    status = field.validation_status
    if status and status not in {"passed", "multiple_occurrences"}:
        issues.append(status)
    for item in (detail.get("unresolved") or []):
        if isinstance(item, dict) and item.get("code"):
            issues.append(str(item["code"]))
        elif item:
            issues.append("UNRESOLVED")
    for instance in field.instances or detail.get("instances") or []:
        if not isinstance(instance, dict):
            continue
        for issue in instance.get("quality_issues") or instance.get("issues") or []:
            issues.append(str(issue.get("code") if isinstance(issue, dict) else issue))
    if (detail.get("coverage") or {}).get("unvisited_pages"):
        issues.append("PARTIAL_COVERAGE")
    return list(dict.fromkeys(code for code in issues if code))


def _audit(row: FieldReviewRevision | None) -> dict | None:
    if row is None:
        return None
    return {
        "review_id": str(row.review_id),
        "revision": row.revision,
        "action": row.action,
        "review_status": "REJECTED" if row.action == "REJECT" else "HUMAN_REVIEWED",
        "reviewer": row.reviewer,
        "reason": row.reason,
        "facts_count": len(row.facts or []),
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


@router.get("", summary="查询可选人工核对工作台")
async def list_review_workspace(
    state: Literal["UNREVIEWED", "HUMAN_REVIEWED", "REJECTED"] = Query("UNREVIEWED"),
    checklist_type: str | None = Query(None, max_length=100),
    mine_name: str | None = Query(None, max_length=200),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    latest_ranked = select(
        FieldReviewRevision.review_id.label("review_id"),
        func.row_number().over(
            partition_by=(FieldReviewRevision.parse_run_id, FieldReviewRevision.field_key),
            order_by=(FieldReviewRevision.revision.desc(), FieldReviewRevision.created_at.desc()),
        ).label("row_number"),
    ).subquery("latest_review_ranked")
    latest_table = (
        select(FieldReviewRevision)
        .join(latest_ranked, latest_ranked.c.review_id == FieldReviewRevision.review_id)
        .where(latest_ranked.c.row_number == 1)
        .subquery("latest_review")
    )
    latest = aliased(FieldReviewRevision, latest_table)

    base = (
        select(ExtractedFieldModel, ParseRun, Document, latest)
        .join(ParseRun, ParseRun.parse_run_id == ExtractedFieldModel.parse_run_id)
        .join(Document, Document.document_id == ExtractedFieldModel.document_id)
        .outerjoin(
            latest,
            and_(
                latest.parse_run_id == ExtractedFieldModel.parse_run_id,
                latest.field_key == ExtractedFieldModel.field_key,
            ),
        )
        .where(ParseRun.status.in_(["COMPLETED", "COMPLETED_WITH_REVIEW", "FAILED"]))
    )
    if checklist_type:
        base = base.where(func.coalesce(ParseRun.checklist_type, Document.checklist_type) == checklist_type)
    if mine_name:
        base = base.where(Document.mine_name.ilike(f"%{mine_name}%"))
    if state == "UNREVIEWED":
        base = base.where(latest.review_id.is_(None))
    elif state == "HUMAN_REVIEWED":
        base = base.where(latest.review_id.is_not(None), latest.action != "REJECT")
    else:
        base = base.where(latest.action == "REJECT")

    count_query = select(func.count()).select_from(base.order_by(None).subquery())
    total = int(await db.scalar(count_query) or 0)
    rows = (await db.execute(
        base.order_by(ParseRun.created_at.desc(), ParseRun.parse_run_id.asc(), ExtractedFieldModel.field_key.asc(), ExtractedFieldModel.field_id.asc())
        .offset((page - 1) * page_size).limit(page_size)
    )).all()
    items: list[dict] = []
    for field, run, document, audit_row in rows:
        audit = _audit(audit_row)
        view = build_fact_view(run, [field], revisions=[audit_row] if audit_row else [])
        risks = list(dict.fromkeys([*_risk_codes(field), *(str(risk["code"]) for fact in view.facts for risk in fact.risk if risk.get("code"))]))
        items.append({
            "parse_run_id": str(run.parse_run_id),
            "document_id": str(document.document_id),
            "file_name": document.file_name or document.original_name,
            "mine_name": document.mine_name,
            "checklist_type": run.checklist_type or document.checklist_type,
            "reporting_period": document.report_period,
            "run_status": run.status,
            "created_at": run.created_at.isoformat() if run.created_at else None,
            "field_key": field.field_key,
            "field_name": field.field_name,
            "field_value": field.field_value,
            "required": bool(field.required),
            "target_type": field.target_type or "fact",
            "target_status": field.target_status or "NOT_RUN",
            "status_reason": field.status_reason or {},
            "validation_status": field.validation_status,
            "fact_count": len(view.facts),
            "risk_codes": risks,
            "risk_count": len(risks),
            "latest_review": audit,
            "result_url": f"/result/{run.parse_run_id}",
        })
    return {"total": total, "page": page, "page_size": page_size, "items": items}
