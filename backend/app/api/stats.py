"""统计 API

工作台与数据分析页面的数据来源，全部来自真实解析记录。
"""

import json
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, case, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import Document, ExtractedFieldModel, ParseRun
from app.services.execution.job_repository import write_extracted_field_counts
from app.services.fact_policy_v2 import VERSION, is_v2
from app.services.fact_service import build_fact_view
from app.storage import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/stats", tags=["统计"])


_REVIEW_STATUSES = {
    "missing",
    "fallback_used",
    "out_of_range",
    "outlier",
    "invalid_format",
    "blacklisted",
    "not_a_number",
    "conflict",
    "low_confidence",
    "review_required",
    "unresolved",
    # 方案 B 的状态：候选待确认、已搜索但不存在、搜索未完成。
    "partial_candidates",
    "not_found",
    "incomplete",
}


def _field_inventory(field: Any, rule: dict[str, Any], *, run=None, invalidations=()) -> dict[str, Any]:
    """统计候选事实及其结构，不据此批准业务使用。"""
    detail = getattr(field, "extraction_detail", None) or {}
    if is_v2(run, detail=detail):
        from types import SimpleNamespace
        from uuid import UUID

        if run is None:
            run = SimpleNamespace(parse_run_id=getattr(field, "parse_run_id", UUID(int=0)), document_id=UUID(int=0),
                contract_version=VERSION, extraction_profile={"rules": [dict(rule, field_key=getattr(field, "field_key", rule.get("field_key", "unknown")))]})
        facts = build_fact_view(run, [field], invalidations=invalidations).facts
        completion = detail.get("field_completion") or detail.get("completion") or {}
        state = completion.get("semantic_state") or detail.get("semantic_state") or getattr(field, "validation_status", None)
        covered = completion.get("coverage_complete", detail.get("coverage_complete")) is True
        unfinished = state not in {"completed", "not_found", "review_required"} or not covered
        review = state == "review_required" or any(not fact.available_for_downstream for fact in facts)
        return {"candidate_found": int(bool(facts)), "fact_count": len(facts),
            "value_group_count": len(facts), "available_for_downstream": int(bool(facts) and all(f.available_for_downstream for f in facts)),
            "availability_unknown": 0, "risk_count": int(review or unfinished), "review_needed": int(review),
            "unfinished": int(unfinished), "not_found": int(state == "not_found" and covered),
            "coverage_complete": int(covered and state in {"completed", "not_found"})}
    status = getattr(field, "validation_status", None)
    def has_value(value):
        return value is not None and not isinstance(value, dict | list) and bool(str(value).strip())

    instances = getattr(field, "instances", None) or detail.get("instances") or []
    groups = detail.get("value_groups") or []
    instances = instances if isinstance(instances, list) else []
    groups = groups if isinstance(groups, list) else []
    instances = [x for x in instances if isinstance(x, dict) and has_value(x.get("value"))]
    groups = [x for x in groups if isinstance(x, dict) and has_value(x.get("value"))]
    configured_default = status == "fallback_used" or getattr(field, "extraction_method", None) == "fallback"
    if configured_default:
        instances, groups = [], []
    legacy = not instances and not groups and not configured_default and status != "missing" and has_value(getattr(field, "field_value", None))
    facts = groups or instances
    registry = detail.get("evidence_registry") or {}
    registry = registry if isinstance(registry, dict) else {}
    required_dims = [key for key, cfg in (rule.get("dimension_schema") or {}).items() if cfg.get("required")]
    agent_config = (rule.get("strategy") or {}).get("agent_extract") or {}
    required_dims = list(dict.fromkeys([*required_dims, *(agent_config.get("identity_dimensions") or [])]))
    # 代理统一标记的 UNVERIFIED/review_required 只是来源状态，
    # 不表示每个发现的事实都必须由操作员审核。
    risk = status in (_REVIEW_STATUSES - {"review_required"}) or status == "blocked"
    risk = risk or any(bool(item.get("quality_issues") or item.get("issues")) for item in instances)
    coverage = detail.get("coverage") or {}
    incomplete = bool(coverage.get("unvisited_pages") or coverage.get("unprocessed_pages") or detail.get("failures"))
    risk = risk or incomplete
    available = 0
    identities: dict[tuple, set[str]] = {}
    for fact in facts:
        dimensions = {
            key: value.get("value") if isinstance(value, dict) else value
            for key, value in (fact.get("dimensions") or {}).items()
        }
        bound = all(has_value(dimensions.get(key)) and str(dimensions[key]).lower() not in {"unknown", "未知"} for key in required_dims)
        refs = [registry[ref] for ref in (fact.get("evidence_ids") or []) if isinstance(ref, str) and ref in registry]
        refs += [ref if isinstance(ref, dict) else registry.get(ref, {}) for ref in (fact.get("evidence_refs") or []) if isinstance(ref, dict | str)]
        # 原生文本与 OCR 实例自带页码和上下文，不依赖代理证据注册表。
        evidence = any(isinstance(ref, dict) and isinstance(ref.get("page_number"), int) and ref["page_number"] > 0 for ref in refs)
        evidence = evidence or (isinstance(fact.get("page_num"), int) and fact["page_num"] > 0 and bool(fact.get("context")))
        unit_ok = not rule.get("unit") or fact.get("unit") == rule["unit"]
        kind_ok = not rule.get("value_kind") or fact.get("value_kind") == rule["value_kind"]
        role_ok = not groups or fact.get("source_role") not in (None, "", "unknown", "未知")
        kind_ok = kind_ok and (not groups or fact.get("value_kind") not in (None, "", "unknown", "未知"))
        format_ok = True
        value = str(fact["value"])
        if rule.get("data_type") in {"float", "number", "integer", "int"}:
            try:
                number = Decimal(value.replace(",", "").replace("，", ""))
                format_ok = number.is_finite() and (rule["data_type"] not in {"integer", "int"} or number == number.to_integral_value())
                value = str(number.normalize()) if format_ok else value
            except InvalidOperation:
                format_ok = False
        available += int(bound and evidence and unit_ok and kind_ok and role_ok and format_ok and not incomplete and status != "blocked")
        issues = fact.get("quality_issues") or fact.get("issues") or []
        risk = risk or bool(issues) or not (bound and evidence and unit_ok and kind_ok and role_ok and format_ok)
        identity_known = bound and (bool(dimensions) or not rule.get("dimension_schema"))
        if identity_known:
            identity = (tuple(sorted((k, str(v)) for k, v in dimensions.items())), fact.get("unit"), fact.get("value_kind"), fact.get("source_role"))
            identities.setdefault(identity, set()).add(value)
    risk = risk or any(len(values) > 1 for values in identities.values())
    candidate_found = bool(facts or legacy)
    return {
        "candidate_found": int(candidate_found),
        "fact_count": len(facts) or int(legacy),
        "value_group_count": len(groups),
        "available_for_downstream": None if legacy else int(available > 0),
        "availability_unknown": int(legacy),
        "risk_count": int(bool(risk)),
        "review_needed": int(bool(risk)),
    }


def _json_value(value):
    if isinstance(value, str):
        return json.loads(value)
    return value


async def _load_quality_fields(db: AsyncSession, checklist_type: str | None):
    # 证据注册表约 200MB，且没有 page_number，不参与这页的统计。
    # 只取统计实际读取的键，避免把整份 extraction_detail 传回应用。
    sql = """
        SELECT f.parse_run_id, f.document_id, f.field_key, f.field_name, f.validation_status,
               f.confidence, f.extraction_method, f.field_value, f.unit, f.required, f.instances,
               f.quality_inputs AS extraction_detail
        FROM extracted_fields f
        JOIN parse_runs r ON r.parse_run_id = f.parse_run_id
        WHERE (r.status = 'COMPLETED'
               OR (r.contract_version = :version
                   AND r.status IN ('COMPLETED_WITH_REVIEW', 'PARTIAL', 'INCOMPLETE')))
    """
    params: dict[str, Any] = {"version": VERSION}
    if checklist_type:
        sql += " AND r.checklist_type = :checklist"
        params["checklist"] = checklist_type
    rows = (await db.execute(text(sql), params)).mappings().all()
    fields = []
    for row in rows:
        data = dict(row)
        data["instances"] = _json_value(data["instances"])
        data["extraction_detail"] = _json_value(data["extraction_detail"]) or {}
        fields.append(SimpleNamespace(**data))
    return fields


def _start_of_today() -> datetime:
    # 旧版数据库时间戳为不带时区的 UTC；先按上海时区计算日期边界，
    # 再转回不带时区的 UTC 进行比较。
    local = datetime.now(ZoneInfo("Asia/Shanghai"))
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(UTC).replace(tzinfo=None)


@router.get("/overview", summary="工作台概览")
async def overview(db: AsyncSession = Depends(get_db)):
    """今日解析量、待处理任务、平均耗时、平均置信度"""
    today = _start_of_today()

    today_count = (
        await db.scalar(
            select(func.count(ParseRun.parse_run_id)).where(ParseRun.created_at >= today)
        )
        or 0
    )

    pending_count = (
        await db.scalar(
            select(func.count(ParseRun.parse_run_id)).where(
                ParseRun.status.in_(["PENDING", "RUNNING", "PROCESSING", "RETRYING"])
            )
        )
        or 0
    )

    completed = (
        await db.scalar(
            select(func.count(ParseRun.parse_run_id)).where(ParseRun.status == "COMPLETED")
        )
        or 0
    )

    failed = (
        await db.scalar(
            select(func.count(ParseRun.parse_run_id)).where(ParseRun.status == "FAILED")
        )
        or 0
    )

    total = await db.scalar(select(func.count(ParseRun.parse_run_id))) or 0

    avg_duration = await db.scalar(
        select(func.avg(ParseRun.duration_seconds)).where(ParseRun.duration_seconds.isnot(None))
    )

    avg_confidence = await db.scalar(
        select(func.avg(ParseRun.mean_confidence)).where(ParseRun.mean_confidence.isnot(None))
    )

    document_count = await db.scalar(select(func.count(Document.document_id))) or 0

    # 昨日解析量，用于计算环比
    yesterday_start = today - timedelta(days=1)
    yesterday_count = (
        await db.scalar(
            select(func.count(ParseRun.parse_run_id)).where(
                ParseRun.created_at >= yesterday_start, ParseRun.created_at < today
            )
        )
        or 0
    )

    trend_percent: float | None = None
    if yesterday_count:
        trend_percent = round((today_count - yesterday_count) / yesterday_count * 100, 1)
    elif today_count:
        trend_percent = 100.0

    # 低置信度任务，需人工复核
    review_count = (
        await db.scalar(
            select(func.count(ParseRun.parse_run_id)).where(
                ParseRun.status == "COMPLETED",
                (ParseRun.mean_confidence < 0.8)
                | ParseRun.quality_status.in_(["REVIEW_REQUIRED", "DEGRADED", "FAILED"]),
            )
        )
        or 0
    )

    return {
        "today_count": today_count,
        "today_trend_percent": trend_percent,
        "yesterday_count": yesterday_count,
        "pending_count": pending_count,
        "completed_count": completed,
        "failed_count": failed,
        "total_runs": total,
        "document_count": document_count,
        "avg_duration_seconds": round(avg_duration, 1) if avg_duration else None,
        "avg_confidence": round(avg_confidence, 4) if avg_confidence else None,
        "review_count": review_count,
        "success_rate": round(completed / total * 100, 1) if total else None,
    }


@router.get("/trend", summary="解析量趋势")
async def trend(days: int = Query(7, ge=1, le=90), db: AsyncSession = Depends(get_db)):
    """按天统计解析量与成功/失败数"""
    start = _start_of_today() - timedelta(days=days - 1)

    result = await db.execute(
        select(
            func.date_trunc("day", ParseRun.created_at + timedelta(hours=8)).label("day"),
            func.count(ParseRun.parse_run_id).label("total"),
            func.sum(case((ParseRun.status == "COMPLETED", 1), else_=0)).label("completed"),
            func.sum(case((ParseRun.status == "FAILED", 1), else_=0)).label("failed"),
            func.sum(
                case(
                    (ParseRun.status.in_(["PENDING", "RUNNING", "PROCESSING", "RETRYING"]), 1),
                    else_=0,
                )
            ).label("pending"),
        )
        .where(ParseRun.created_at >= start)
        .group_by("day")
        .order_by("day")
    )

    rows = {row.day.date(): row for row in result.all()}

    series = []
    for offset in range(days):
        day = (start + timedelta(hours=8, days=offset)).date()
        row = rows.get(day)
        series.append(
            {
                "date": day.isoformat(),
                "total": int(row.total) if row else 0,
                "completed": int(row.completed or 0) if row else 0,
                "failed": int(row.failed or 0) if row else 0,
                "pending": int(row.pending or 0) if row else 0,
            }
        )

    return {"days": days, "series": series}


@router.get("/checklist-distribution", summary="清单类型分布")
async def checklist_distribution(db: AsyncSession = Depends(get_db)):
    """按清单类型统计文档数、完成数与平均置信度"""
    result = await db.execute(
        select(
            Document.checklist_type,
            func.count(Document.document_id).label("documents"),
        )
        .group_by(Document.checklist_type)
        .order_by(func.count(Document.document_id).desc())
    )
    document_rows = result.all()

    run_result = await db.execute(
        select(
            Document.checklist_type,
            func.count(ParseRun.parse_run_id).label("runs"),
            func.sum(case((ParseRun.status == "COMPLETED", 1), else_=0)).label("completed"),
            func.avg(ParseRun.mean_confidence).label("avg_confidence"),
            func.avg(ParseRun.duration_seconds).label("avg_duration"),
        )
        .join(ParseRun, ParseRun.document_id == Document.document_id)
        .group_by(Document.checklist_type)
    )
    run_rows = {row.checklist_type: row for row in run_result.all()}

    items = []
    for row in document_rows:
        stats = run_rows.get(row.checklist_type)
        items.append(
            {
                "checklist_type": row.checklist_type or "未标注清单",
                "documents": int(row.documents),
                "runs": int(stats.runs) if stats else 0,
                "completed": int(stats.completed or 0) if stats else 0,
                "avg_confidence": (
                    round(stats.avg_confidence, 4) if stats and stats.avg_confidence else None
                ),
                "avg_duration_seconds": (
                    round(stats.avg_duration, 1) if stats and stats.avg_duration else None
                ),
            }
        )
    return {"items": items, "total_documents": sum(i["documents"] for i in items)}


@router.get("/confidence-distribution", summary="置信度分布")
async def confidence_distribution(db: AsyncSession = Depends(get_db)):
    """按区间统计解析运行的平均置信度分布"""
    result = await db.execute(
        select(
            case(
                (ParseRun.mean_confidence >= 0.95, "95% 及以上"),
                (ParseRun.mean_confidence >= 0.85, "85% – 95%"),
                (ParseRun.mean_confidence >= 0.7, "70% – 85%"),
                else_="70% 以下",
            ).label("bucket"),
            func.count(ParseRun.parse_run_id).label("count"),
        )
        .where(ParseRun.mean_confidence.isnot(None))
        .group_by("bucket")
    )
    rows = {row.bucket: int(row._mapping["count"]) for row in result.all()}
    order = ["95% 及以上", "85% – 95%", "70% – 85%", "70% 以下"]
    total = sum(rows.values())
    return {
        "items": [
            {
                "label": label,
                "count": rows.get(label, 0),
                "ratio": round(rows.get(label, 0) / total * 100, 1) if total else 0.0,
            }
            for label in order
        ],
        "total": total,
    }


@router.get("/field-quality", summary="字段提取质量")
async def field_quality(
    checklist_type: str | None = Query(None),
    limit: int = Query(30, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """按运行快照统计字段发现、结构可消费及风险；不代表业务放行。"""
    # 覆盖率分母为所选清单中，快照包含该字段且已完成的运行数；
    # 不计入旧运行或运行创建后才新增的字段。
    observed = or_(ParseRun.status == "COMPLETED", and_(ParseRun.contract_version == VERSION,
        ParseRun.status.in_(["COMPLETED_WITH_REVIEW", "PARTIAL", "INCOMPLETE"])))
    runs_query = select(ParseRun).where(observed)
    if checklist_type:
        runs_query = runs_query.where(ParseRun.checklist_type == checklist_type)
    completed_runs = (await db.execute(runs_query)).scalars().all()
    dialect = getattr(getattr(getattr(db, "bind", None), "dialect", None), "name", None)
    if dialect == "postgresql":
        # 证据原文和完成证明占了字段 JSON 的绝大部分，质量统计用不到。
        # 在数据库里裁掉后再传，避免每次打开页面搬运上百 MB。
        fields = await _load_quality_fields(db, checklist_type)
    else:
        fields_query = (
            select(ExtractedFieldModel)
            .join(ParseRun, ParseRun.parse_run_id == ExtractedFieldModel.parse_run_id)
            .where(observed)
        )
        if checklist_type:
            fields_query = fields_query.where(ParseRun.checklist_type == checklist_type)
        fields = (await db.execute(fields_query)).scalars().all()
    fields_by_run = {(field.parse_run_id, field.field_key): field for field in fields}
    grouped: dict[tuple[str, str], dict] = {}
    invalidations_by_document: dict[str, list[Any]] = {}
    for run in completed_runs:
        invalidations = []
        if is_v2(run) and getattr(run, "document_id", None):
            from app.services.fact_policy_v2 import load_invalidations

            document_key = str(run.document_id)
            if document_key not in invalidations_by_document:
                invalidations_by_document[document_key] = await load_invalidations(db, run.document_id)
            invalidations = invalidations_by_document[document_key]
        snapshot = (run.extraction_profile or {}).get("rules")
        # 旧版运行没有快照：展示观测值，但覆盖率保持未知；
        # 不能用当前规则替代历史要求。
        known = isinstance(snapshot, list)
        rules: list[dict[str, Any]] = (
            snapshot
            if isinstance(snapshot, list)
            else [
                {
                    "field_key": field.field_key,
                    "field_name_cn": field.field_name,
                    "required": field.required,
                }
                for field in fields
                if field.parse_run_id == run.parse_run_id
            ]
        )
        for rule in rules:
            key = rule.get("field_key")
            if not isinstance(key, str) or not key:
                continue
            bucket = grouped.setdefault(
                (run.checklist_type or "", key),
                {
                    "field_key": key,
                    "field_name": rule.get("field_name_cn") or key,
                    "checklist_type": run.checklist_type,
                    "expected": 0,
                    "extracted": 0,
                    "observed": 0,
                    "candidate_found": 0,
                    "fact_count": 0,
                    "value_group_count": 0,
                    "available_for_downstream": 0,
                    "availability_unknown": 0,
                    "risk_count": 0,
                    "confidence": [],
                    "review_needed": 0,
                    "unfinished": 0,
                    "not_found": 0,
                    "scope_completed": 0,
                },
            )
            if known:
                bucket["expected"] += 1
            field = fields_by_run.get((run.parse_run_id, key))
            inventory: dict[str, Any] = _field_inventory(field, rule, run=run, invalidations=invalidations) if field is not None else {
                "candidate_found": 0,
                "fact_count": 0,
                "value_group_count": 0,
                "available_for_downstream": 0,
                "availability_unknown": 0,
                "risk_count": 0,
                "review_needed": 0,
                "unfinished": int(is_v2(run)),
            }
            if not known and inventory["candidate_found"]:
                inventory["available_for_downstream"] = None
                inventory["availability_unknown"] = 1
            present = bool(inventory["candidate_found"])
            bucket["candidate_found"] += inventory["candidate_found"]
            bucket["fact_count"] += inventory["fact_count"]
            bucket["value_group_count"] += inventory["value_group_count"]
            if inventory["available_for_downstream"] is not None:
                bucket["available_for_downstream"] += inventory["available_for_downstream"]
            bucket["availability_unknown"] += inventory["availability_unknown"]
            bucket["risk_count"] += inventory["risk_count"]
            bucket["unfinished"] += inventory.get("unfinished", 0)
            bucket["not_found"] += inventory.get("not_found", 0)
            bucket["scope_completed"] += inventory.get("coverage_complete", 0)
            if present:
                bucket["observed"] += 1
                if known:
                    bucket["extracted"] += 1
                if field is not None and field.confidence is not None:
                    bucket["confidence"].append(field.confidence)
            invalid = field is not None and (
                bool(inventory.get("review_needed"))
                or getattr(field, "validation_status", None) in (_REVIEW_STATUSES - {"review_required"})
                or (field.confidence is not None and field.confidence < 0.8)
            )
            if invalid or (not present and rule.get("required") and not inventory.get("not_found")):
                bucket["review_needed"] += 1
                if not inventory["risk_count"]:
                    bucket["risk_count"] += 1
    items = []
    for bucket in grouped.values():
        confidence = bucket.pop("confidence")
        expected = bucket.pop("expected")
        covered = bucket.pop("extracted")
        bucket["extracted"] = bucket.pop("observed")
        if bucket["availability_unknown"] and not bucket["available_for_downstream"]:
            bucket["available_for_downstream"] = None
        # candidate_found 是新的明确字段名；extracted 作为旧仪表盘兼容别名保留。
        bucket["coverage"] = round(covered / expected * 100, 1) if expected else None
        bucket["completion_coverage"] = round(bucket.pop("scope_completed") / expected * 100, 1) if expected else None
        bucket["avg_confidence"] = (
            round(sum(confidence) / len(confidence), 4) if confidence else None
        )
        bucket["min_confidence"] = round(min(confidence), 4) if confidence else None
        items.append(bucket)
    items.sort(key=lambda item: (-item["review_needed"], -item["extracted"], item["field_key"]))
    reliable = any(is_v2(run) for run in completed_runs)
    return {"items": items[:limit], "completed_runs": sum(getattr(run, "status", "COMPLETED") == "COMPLETED" for run in completed_runs),
            "observed_runs": len(completed_runs),
            "availability_policy": "complete_scope_v2" if reliable else "structural_v1",
            "availability_meaning": "Facts and source evidence available for downstream assessment; not business approval."}


@router.get("/stage-duration", summary="链路耗时分布")
async def stage_duration(db: AsyncSession = Depends(get_db)):
    """按解析阶段统计真实耗时（来自 parse_logs.details.duration_seconds）"""
    from app.models.database import ParseLog

    result = await db.execute(select(ParseLog.stage, ParseLog.details))
    durations: dict[str, list[float]] = {}
    for stage, details in result.all():
        details_dict: dict[str, Any] = details if isinstance(details, dict) else {}
        value = details_dict.get("duration_seconds")
        if isinstance(value, int | float) and value >= 0:
            durations.setdefault(str(stage), []).append(float(value))

    avg_total = await db.scalar(
        select(func.avg(ParseRun.duration_seconds)).where(ParseRun.duration_seconds.isnot(None))
    )

    return {
        "stages": [
            {
                "stage": stage,
                "entries": len(values),
                "avg_duration_seconds": round(sum(values) / len(values), 3),
            }
            for stage, values in sorted(durations.items(), key=lambda item: -len(item[1]))
        ],
        "avg_total_seconds": round(avg_total, 1) if avg_total else None,
    }


@router.get("/recent-runs", summary="最近解析任务")
async def recent_runs(limit: int = Query(8, ge=1, le=50), db: AsyncSession = Depends(get_db)):
    """工作台最近任务列表"""
    result = await db.execute(
        select(Document, ParseRun)
        .join(ParseRun, ParseRun.document_id == Document.document_id)
        .order_by(ParseRun.created_at.desc())
        .limit(limit)
    )
    rows = result.all()
    await write_extracted_field_counts(db, [run for _, run in rows], repair_zeros=True)

    items = []
    for document, run in rows:
        items.append(
            {
                "parse_run_id": str(run.parse_run_id),
                "document_id": str(document.document_id),
                "file_name": document.file_name,
                "mine_name": document.mine_name,
                "checklist_type": document.checklist_type,
                "reporting_period": document.report_period,
                "status": run.status,
                "fields_extracted": run.fields_extracted,
                "mean_confidence": run.mean_confidence,
                "duration_seconds": run.duration_seconds,
                "created_at": run.created_at,
            }
        )
    return {"items": items}
