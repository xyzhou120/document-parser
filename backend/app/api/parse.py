"""
API路由 - 文档解析相关接口
"""

import asyncio
import csv
import hashlib
import io
import json
import logging
import re
from typing import Annotated, Literal
from urllib.parse import quote
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.database import Document, ExtractedFieldModel, ParseRun
from app.models.reviews import FieldReviewRevision
from app.rules.repository import rule_repository
from app.schemas.parse import (
    ExtractionRequest,
    FactViewResponse,
    ParseFieldResponse,
    ParseResultItem,
    ParseResultResponse,
    ParseRunDetailResponse,
    ReliableUploadResponse,
    StartParseResponse,
)
from app.services.artifact_service import artifact_service, preprocessing_profile
from app.services.execution.job_repository import write_extracted_field_counts
from app.services.fact_service import (
    build_fact_view,
    build_fact_view_with_invalidations,
    build_run_facts,
    compare_fact_views,
)
from app.services.parse_service import changed_field_keys, rule_snapshot
from app.storage import get_db, minio_client
from app.tasks import parse_document_task

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/parse", tags=["解析"])


@router.get("/capabilities", summary="查询本地解析能力")
async def parse_capabilities():
    """返回本地已启用的处理路径，供上传界面明确选择。"""
    reliable = settings.app.reliable_enabled
    return {
        "reliable_enabled": reliable,
        "assistant_enabled": settings.app.assistant_enabled,
        "default_extraction_engine": "reliable" if reliable else "checklist_agent",
        "extraction_engines": ["reliable", "checklist_agent", "rules"] if reliable else ["checklist_agent", "rules"],
    }


@router.post("/upload-and-parse", response_model=ReliableUploadResponse | StartParseResponse, summary="上传文档并启动解析")
async def upload_and_parse(
    file: UploadFile = File(..., description="PDF文件"),
    mine_id: str = Form(..., description="煤矿ID"),
    mine_name: str = Form(..., description="煤矿名称"),
    checklist_type: str = Form(..., description="清单类型"),
    reporting_period: str = Form(..., description="上报周期，格式：YYYY-MM"),
    extraction_engine: Annotated[str | None, Form(description="提取引擎")] = None,
    db: AsyncSession = Depends(get_db),
):
    """
    上传文档并启动解析

    工作流程：
    1. 上传文件到MinIO
    2. 创建Document记录
    3. 创建ParseRun记录
    4. 提交Celery异步任务
    5. 返回parse_run_id
    """
    object_key = None
    persisted = False
    try:
        # 1. 验证文件类型
        if not file.filename or not file.filename.lower().endswith(".pdf"):
            raise HTTPException(status_code=400, detail="只支持PDF文件")
        if not mine_id.strip() or not mine_name.strip():
            raise HTTPException(status_code=400, detail="煤矿编号与名称不能为空")
        if len(mine_id) > 100 or len(mine_name) > 200 or len(file.filename) > 500:
            raise HTTPException(status_code=422, detail="文件名或煤矿信息超过允许长度")
        available = await rule_repository.list_checklist_types(db, only_active=True)
        if checklist_type not in {item["checklist_type"] for item in available}:
            raise HTTPException(status_code=422, detail="清单类型不存在或已停用")
        rules = await rule_repository.get_rules_for_checklist(db, checklist_type)
        if not rules:
            raise HTTPException(status_code=422, detail="该清单尚未配置启用规则")
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", reporting_period):
            raise HTTPException(status_code=400, detail="上报周期格式应为YYYY-MM")
        selected_engine = extraction_engine or ("reliable" if settings.app.reliable_enabled else "checklist_agent")
        if selected_engine not in {"reliable", "checklist_agent", "rules"}:
            raise HTTPException(status_code=422, detail="提取引擎不受支持")
        if selected_engine == "reliable" and not settings.app.reliable_enabled:
            raise HTTPException(status_code=409, detail="可靠提取未启用，请在本地配置 app.reliable_enabled=true 后重启 API 和 worker")
        if selected_engine == "rules" and any(getattr(rule, "methods", []) == ["agent_extract"] for rule in rules):
            raise HTTPException(status_code=422, detail="此清单包含智能体专用字段，请选择智能体引擎")

        # 2. 上传到MinIO
        max_size = min(settings.upload.max_file_size, 100 * 1024 * 1024)
        file_content = await file.read(max_size + 1)
        if len(file_content) > max_size:
            raise HTTPException(status_code=413, detail=f"文件不能超过{max_size // (1024 * 1024)}MB")
        if not file_content.startswith(b"%PDF-"):
            raise HTTPException(status_code=400, detail="文件内容不是有效PDF")
        object_key = f"uploads/{uuid4()}.pdf"
        minio_url = await asyncio.to_thread(minio_client.upload, object_key, file_content)

        logger.info(f"Uploaded file to MinIO: {minio_url}")

        # 3. 创建Document
        document = Document(
            file_name=file.filename,
            original_name=file.filename,
            file_path=object_key,
            file_size=len(file_content),
            file_hash=hashlib.sha256(file_content).hexdigest(),
            mime_type="application/pdf",
            mine_id=mine_id,
            mine_name=mine_name,
            checklist_type=checklist_type,
            report_period=reporting_period,
        )
        db.add(document)
        await db.flush()

        # 本地 v2 验证模式直接写入可靠执行账本，此分支不创建旧版 ParseRun，
        # 避免将结果误认为旧版执行。
        if selected_engine == "reliable":
            from app.services.reliable_extraction_service import (
                ContentPreparationRequired,
                reliable_extraction_service,
            )

            request_id = f"upload:{document.document_id}"
            try:
                reliable_run = await reliable_extraction_service.start(
                    db, document.document_id, rules=rules, request_id=request_id
                )
            except ContentPreparationRequired as exc:
                await db.commit()
                persisted = True
                from app.tasks import reliable_content_build_task
                try:
                    reliable_content_build_task.apply_async(args=[str(exc.build_id)], queue="reliable-execution")
                except Exception:
                    # 持久化的 content.build.execute 发件箱负责恢复。
                    pass
                return ReliableUploadResponse(
                    parse_run_id=None,
                    document_id=document.document_id,
                    status="PREPARING_CONTENT",
                    message="文档已上传，正在准备 v2 全文内容",
                    contract_version="reliable-extraction-v2",
                    extraction_engine="reliable",
                    build_id=exc.build_id,
                )

            await db.commit()
            persisted = True
            from app.tasks import reliable_execution_wakeup_task
            try:
                reliable_execution_wakeup_task.apply_async(
                    args=[str(reliable_run.parse_run_id)], queue="reliable-execution"
                )
            except Exception:
                # field_job_wakeup 发件箱保持待处理，以便定时任务恢复。
                pass
            return ReliableUploadResponse(
                parse_run_id=reliable_run.parse_run_id,
                document_id=document.document_id,
                status=reliable_run.status,
                message="v2 可靠提取任务已提交",
                contract_version="reliable-extraction-v2",
                extraction_engine="reliable",
            )

        # 4. 创建ParseRun
        parse_run = ParseRun(
            document_id=document.document_id,
            status="PENDING",
            run_mode="FULL",
            extraction_profile=rule_snapshot(rules, checklist_type, selected_engine),
        )
        db.add(parse_run)
        await db.commit()
        persisted = True

        # 5. 提交Celery任务
        try:
            task = parse_document_task.delay(str(document.document_id), str(parse_run.parse_run_id))
        except Exception as exc:
            parse_run.status = "FAILED"
            parse_run.error_message = "提交到Celery队列失败，请检查Redis和Worker后重新上传"
            await db.commit()
            raise HTTPException(status_code=503, detail=parse_run.error_message) from exc

        logger.info(
            f"Started parse task: parse_run_id={parse_run.parse_run_id}, "
            f"celery_task_id={task.id}"
        )

        return StartParseResponse(
            parse_run_id=parse_run.parse_run_id,
            document_id=document.document_id,
            status="PENDING",
            message="解析任务已提交",
            extraction_engine=selected_engine,
        )

    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        if object_key and not persisted:
            try:
                await asyncio.to_thread(minio_client.delete, object_key)
            except Exception:
                logger.warning("清理未持久化文档失败: %s", object_key, exc_info=True)
        logger.error(f"Upload and parse failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="上传解析失败，请检查日志和依赖服务状态") from e


@router.get(
    "/runs/{parse_run_id}", response_model=ParseRunDetailResponse, summary="查询解析运行详情"
)
async def get_parse_run(
    parse_run_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """
    查询解析运行详情

    返回：
    - 解析运行信息
    - 文档信息
    - 提取的字段列表
    """
    # 查询ParseRun
    parse_run = await db.get(ParseRun, parse_run_id)
    if not parse_run:
        raise HTTPException(status_code=404, detail="解析运行不存在")

    # 查询Document
    document = await db.get(Document, parse_run.document_id)

    # 查询ExtractedFields
    result = await db.execute(
        select(ExtractedFieldModel).where(ExtractedFieldModel.parse_run_id == parse_run_id)
    )
    fields = result.scalars().all()
    await write_extracted_field_counts(db, [parse_run], repair_zeros=True)

    return ParseRunDetailResponse(
        parse_run_id=parse_run.parse_run_id,
        document_id=parse_run.document_id,
        status=parse_run.status,
        contract_version=getattr(parse_run, "contract_version", None),
        content_revision_id=getattr(parse_run, "content_revision_id", None),
        execution_generation=getattr(parse_run, "execution_generation", None),
        started_at=parse_run.started_at,
        completed_at=parse_run.completed_at,
        duration_seconds=parse_run.duration_seconds,
        fields_extracted=parse_run.fields_extracted,
        mean_confidence=parse_run.mean_confidence,
        error_message=parse_run.error_message,
        quality_status=parse_run.quality_status,
        extraction_profile=parse_run.extraction_profile,
        missing_fields=(parse_run.extraction_profile or {}).get("missing_fields", []),
        warnings=(parse_run.extraction_profile or {}).get("warnings", []),
        document_name=document.file_name if document else None,
        file_hash=document.file_hash if document else None,
        mine_name=document.mine_name if document else None,
        checklist_type=document.checklist_type if document else None,
        reporting_period=document.report_period if document else None,
        facts=build_run_facts(parse_run, fields),
        fields=[
            ParseFieldResponse.model_validate(
                {
                    "field_key": f.field_key,
                    "field_name": f.field_name,
                    "field_value": f.field_value,
                    "extraction_method": f.extraction_method,
                    "confidence": f.confidence,
                    "data_type": f.data_type,
                    "unit": f.unit,
                    "required": f.required,
                    "validation_status": f.validation_status,
                    "instances": f.instances,
                    "rule_id": f.rule_id,
                    "rule_version": f.rule_version,
                    "extraction_detail": f.extraction_detail,
                    "evidence": f.evidence,
                }
            )
            for f in fields
        ],
        artifact_id=getattr(parse_run, "artifact_id", None),
        parent_parse_run_id=getattr(parse_run, "parent_parse_run_id", None),
        mode=getattr(parse_run, "run_mode", None),
        cache_status=getattr(parse_run, "cache_status", None),
        artifact_status=(parse_run.extraction_profile or {}).get("artifact_status"),
    )


async def _run_fact_view(parse_run_id: UUID, view: str, db: AsyncSession):
    run = await db.get(ParseRun, parse_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="解析运行不存在")
    result = await db.execute(
        select(ExtractedFieldModel).where(ExtractedFieldModel.parse_run_id == parse_run_id)
        .order_by(ExtractedFieldModel.field_key, ExtractedFieldModel.field_id)
    )
    fields = result.scalars().all()
    result = await db.execute(
        select(FieldReviewRevision).where(FieldReviewRevision.parse_run_id == parse_run_id)
        .order_by(FieldReviewRevision.field_key, FieldReviewRevision.revision.desc())
    )
    revisions = result.scalars().all()
    result_view = (await build_fact_view_with_invalidations(db, run, fields, view=view, revisions=revisions)
                   if getattr(run, "contract_version", None) == "reliable-extraction-v2" else
                   build_fact_view(run, fields, view=view, revisions=revisions))
    if getattr(run, "artifact_id", None):
        from app.services.source_revision_service import source_state
        state = await source_state(db, run)
        for fact in result_view.facts:
            fact.provenance["source_state"] = state
            if state.get("stale_source"):
                fact.risk.append({"code": "STALE_SOURCE_REVISION", "revision_ids": state["revision_ids"]})
                fact.available_for_downstream = False
            if state.get("source_unreadable_pages"):
                fact.risk.append({"code": "SOURCE_UNREADABLE_PAGES", "pages": state["source_unreadable_pages"]})
    return result_view


@router.get("/runs/{parse_run_id}/facts", response_model=FactViewResponse,
            summary="统一事实视图（自动或显式人工复核）")
async def get_run_facts(
    parse_run_id: UUID,
    view: Literal["automatic", "reviewed", "available"] = "automatic",
    db: AsyncSession = Depends(get_db),
):
    return await _run_fact_view(parse_run_id, view, db)


@router.get("/runs/{parse_run_id}/compare", summary="比较同文档两次运行的完整事实")
async def compare_runs(
    parse_run_id: UUID,
    other_run_id: UUID,
    view: Literal["automatic", "reviewed"] = "automatic",
    db: AsyncSession = Depends(get_db),
):
    before = await _run_fact_view(other_run_id, view, db)
    after = await _run_fact_view(parse_run_id, view, db)
    if before.document_id != after.document_id:
        raise HTTPException(status_code=422, detail="只能比较同一文档的两次运行")
    return compare_fact_views(before, after)


@router.get("/runs/{parse_run_id}/facts/export", summary="完整事实导出（不受分页限制）")
async def export_run_facts(
    parse_run_id: UUID,
    format: Literal["json", "csv"] = "json",
    view: Literal["automatic", "reviewed"] = "automatic",
    db: AsyncSession = Depends(get_db),
):
    result = await _run_fact_view(parse_run_id, view, db)
    headers = {"Content-Disposition": f'attachment; filename="facts-{parse_run_id}-{view}.{format}"',
               "Cache-Control": "private, no-store"}
    if format == "json":
        return Response(content=result.model_dump_json(), media_type="application/json", headers=headers)
    columns = ["parse_run_id", "document_id", "view", "fact_id", "field_key", "field_name", "raw",
               "value", "normalized", "unit", "dimensions", "value_kind", "source_role", "evidence",
               "confidence", "checks", "risk", "coverage", "provenance", "available_for_downstream", "review_status", "field_review_status"]
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=columns)
    writer.writeheader()
    for fact in result.facts:
        row = {key: val for key, val in fact.model_dump(mode="json").items() if key in columns}
        row.update(parse_run_id=str(parse_run_id), document_id=str(result.document_id), view=view)
        for key, val in row.items():
            if isinstance(val, dict | list):
                row[key] = json.dumps(val, ensure_ascii=False, sort_keys=True)
            elif isinstance(val, str) and val.lstrip().startswith(("=", "+", "-", "@")):
                # 导出的源文本不能触发表格公式执行。
                row[key] = "'" + val
        writer.writerow(row)
    return Response(content=("\ufeff" + stream.getvalue()).encode("utf-8"),
                    media_type="text/csv; charset=utf-8", headers=headers)


@router.post("/documents/{document_id}/extract", response_model=StartParseResponse,
             summary="按已有文档补充字段（增量提取）")
async def incremental_extract(
    document_id: UUID,
    payload: ExtractionRequest,
    db: AsyncSession = Depends(get_db),
):
    """创建不可变的子运行，并复用预处理产物。"""
    document = await db.get(Document, document_id)
    if not document:
        raise HTTPException(status_code=404, detail="文档不存在")
    parent = await db.scalar(
        select(ParseRun).where(ParseRun.document_id == document_id, ParseRun.status == "COMPLETED")
        .order_by(ParseRun.created_at.desc()).limit(1)
    )
    if not parent:
        raise HTTPException(status_code=409, detail="文档尚无完成的全量解析运行")
    if not document.checklist_type:
        raise HTTPException(status_code=409, detail="文档缺少清单类型")
    if payload.rule_bundle_id:
        from app.services.release_service import bundle_specs, release_service, validate_bundle
        bundle = await release_service.get(db, payload.rule_bundle_id)
        await release_service._checklist(db, document.checklist_type)
        if bundle.checklist_type != document.checklist_type or not validate_bundle(bundle)["valid"]:
            raise HTTPException(status_code=422, detail="规则发布包与文档清单不匹配或校验失败")
        rules = bundle_specs(bundle)
    else:
        rules = await rule_repository.get_rules_for_document(db, document)
    if not rules:
        raise HTTPException(status_code=422, detail="清单没有启用的提取规则")
    field_keys = list(dict.fromkeys(payload.field_keys or changed_field_keys(rules, parent.extraction_profile or {})))
    if (payload.rule_bundle_id or payload.agent_limits) and not payload.field_keys:
        # 试运行是明确的执行请求，即使草稿规则相同也必须执行；
        # 不能静默跳过，也不能继承其他试运行预算下生成的事实。
        field_keys = [rule.field_key for rule in rules]
    unknown = set(field_keys) - {r.field_key for r in rules}
    if unknown:
        raise HTTPException(status_code=422, detail=f"字段没有启用规则: {sorted(unknown)}")
    removed_keys = {r["field_key"] for r in (parent.extraction_profile or {}).get("rules", [])} - {r.field_key for r in rules}
    extraction_engine = (parent.extraction_profile or {}).get("extraction_engine", "rules")
    if payload.extraction_engine:
        extraction_engine = payload.extraction_engine
    elif payload.rule_bundle_id and any("agent_extract" in rule.methods for rule in rules):
        extraction_engine = "checklist_agent"
    if extraction_engine != (parent.extraction_profile or {}).get("extraction_engine", "rules"):
        field_keys = [rule.field_key for rule in rules]
    if extraction_engine == "rules" and any(rule.methods == ["agent_extract"] for rule in rules):
        raise HTTPException(status_code=422, detail="此规则包需要智能体引擎；请选择智能体并按需强制刷新")
    current_profile = rule_snapshot(rules, document.checklist_type, extraction_engine)
    if extraction_engine == "checklist_agent" and any(
        current_profile.get(key) != (parent.extraction_profile or {}).get(key)
        for key in ("agent_config", "agent_model")
    ):
        # 模型或预算变更会使字段结果失效，但不影响页面产物。
        field_keys = [rule.field_key for rule in rules]
    artifact = await artifact_service.find_ready(
        db, document_id, document.file_hash, preprocessing_profile(extraction_engine)["version"]
    )
    # 刷新会完整重新提取，不能继承旧页面几何位置对应的结果。
    mode = "FULL" if payload.force_refresh else "INCREMENTAL"
    if not payload.force_refresh and (artifact is None or artifact.artifact_id != parent.artifact_id):
        raise HTTPException(status_code=409, detail="无兼容此引擎/版本的完整前处理产物；请选择强制刷新执行全量解析")
    if not payload.force_refresh and not field_keys and not removed_keys:
        raise HTTPException(status_code=409, detail="启用规则没有变化，无需增量提取")
    profile = {**current_profile, "force_refresh": payload.force_refresh}
    if payload.rule_bundle_id:
        profile["rule_bundle_id"] = str(payload.rule_bundle_id)
    if payload.agent_limits:
        if extraction_engine != "checklist_agent":
            raise HTTPException(status_code=422, detail="仅智能体引擎支持取证预算")
        profile["agent_config"] = {**profile.get("agent_config", {}), **payload.agent_limits}
        if field_keys is None or not field_keys:
            field_keys = [rule.field_key for rule in rules]
    run = ParseRun(document_id=document_id, parent_parse_run_id=parent.parse_run_id,
                   requested_field_keys=field_keys if mode == "INCREMENTAL" else None, run_mode=mode,
                   artifact_id=artifact.artifact_id if artifact and mode == "INCREMENTAL" else None,
                   cache_status="ARTIFACT_HIT" if mode == "INCREMENTAL" else "REFRESH",
                   extraction_profile=profile, status="PENDING")
    db.add(run)
    await db.commit()
    try:
        parse_document_task.delay(str(document_id), str(run.parse_run_id), run.requested_field_keys, payload.force_refresh)
    except Exception as exc:
        run.status = "FAILED"
        run.error_message = "提交解析任务失败"
        await db.commit()
        raise HTTPException(status_code=503, detail=run.error_message) from exc
    return StartParseResponse(parse_run_id=run.parse_run_id, document_id=document_id,
                              status="PENDING", message="增量字段提取任务已提交",
                              parent_parse_run_id=parent.parse_run_id, mode=mode,
                              artifact_id=run.artifact_id, cache_status=run.cache_status,
                              artifact_status=artifact.status if artifact else None)


@router.post("/documents/{document_id}/resume", response_model=StartParseResponse,
             summary="复用前处理补跑未完成取证字段")
async def resume_extraction(document_id: UUID, payload: ExtractionRequest,
                            db: AsyncSession = Depends(get_db)):
    parent = await db.scalar(select(ParseRun).where(ParseRun.document_id == document_id,
        ParseRun.status == "COMPLETED").order_by(ParseRun.created_at.desc()).limit(1))
    if parent is None:
        raise HTTPException(status_code=409, detail="尚无可续跑的完成运行")
    profile = parent.extraction_profile or {}
    coverage = (profile.get("agent_runtime") or {}).get("field_coverage", {})
    pending = [rule["field_key"] for rule in profile.get("rules", []) if (
        not coverage.get(rule["field_key"]) or
        coverage[rule["field_key"]].get("unvisited_pages") or
        coverage[rule["field_key"]].get("status") in {"UNRESOLVED", "PARTIAL"})]
    if not pending and not payload.field_keys:
        raise HTTPException(status_code=409, detail="无标记为未完成的字段，可显式选择字段重新提取")
    request = payload.model_copy(update={"field_keys": payload.field_keys or pending})
    return await incremental_extract(document_id, request, db)


@router.get("/results", response_model=ParseResultResponse, summary="查询解析结果（按附件）")
async def get_parse_results(
    mine_id: str | None = None,
    checklist_type: str | None = None,
    reporting_period: str | None = None,
    limit: int = 20,
    offset: int = 0,
    mine_name: str | None = None,
    status: str | None = None,
    page: int | None = Query(None, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """
    查询解析结果

    支持按煤矿ID、清单类型、上报周期筛选
    """
    latest = select(
        ParseRun.parse_run_id,
        func.row_number()
        .over(partition_by=ParseRun.document_id, order_by=ParseRun.created_at.desc())
        .label("rank"),
    ).subquery()
    query = (
        select(Document, ParseRun)
        .join(ParseRun, Document.document_id == ParseRun.document_id)
        .join(latest, latest.c.parse_run_id == ParseRun.parse_run_id)
        .where(latest.c.rank == 1)
    )

    if mine_id:
        query = query.where(Document.mine_id == mine_id)
    if checklist_type:
        query = query.where(Document.checklist_type == checklist_type)
    if reporting_period:
        query = query.where(Document.report_period == reporting_period)
    if mine_name:
        query = query.where(Document.mine_name.contains(mine_name))
    if status:
        query = query.where(ParseRun.status == status)

    total = await db.scalar(select(func.count()).select_from(query.subquery())) or 0
    if page is not None:
        limit, offset = page_size, (page - 1) * page_size
    query = (
        query.limit(min(max(limit, 1), 100))
        .offset(max(offset, 0))
        .order_by(Document.created_at.desc())
    )

    result = await db.execute(query)
    documents = result.all()
    await write_extracted_field_counts(
        db, [parse_run for _, parse_run in documents], repair_zeros=True,
    )

    # 查询每个附件的解析结果
    items = []
    for document, parse_run in documents:
        items.append(
            {
                "document_id": document.document_id,
                "file_name": document.file_name,
                "file_hash": document.file_hash,
                "mine_id": document.mine_id,
                "mine_name": document.mine_name,
                "checklist_type": document.checklist_type,
                "reporting_period": document.report_period,
                "parse_run_id": parse_run.parse_run_id,
                "status": parse_run.status,
                "fields_extracted": parse_run.fields_extracted,
                "mean_confidence": parse_run.mean_confidence,
                "duration_seconds": parse_run.duration_seconds,
                "created_at": document.created_at,
            }
        )

    return ParseResultResponse(
        total=total, items=[ParseResultItem.model_validate(item) for item in items]
    )


@router.get("/rules", summary="读取当前生效的字段规则")
async def get_rules(
    checklist_type: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """读取规则

    优先返回数据库中的启用规则（按清单类型过滤）；
    数据库无规则时回退到配置文件，保持向后兼容。
    """
    rules = await rule_repository.list_rules(db, checklist_type=checklist_type, only_enabled=False)
    if rules:
        return [
            {
                **rule_repository.rule_to_dict(rule),
                "field_type": (rule.data_type or "string").upper(),
                "extraction_methods": (rule.strategy or {}).get("methods", []),
            }
            for rule in rules
        ]
    return [] if checklist_type else settings.fields.get("default", [])


@router.get("/checklist-types", summary="可选清单类型")
async def list_checklist_types(db: AsyncSession = Depends(get_db)):
    """上传表单的清单类型选项，来自数据库配置而非写死"""
    return await rule_repository.list_checklist_types(db, only_active=True)


@router.get("/documents/{document_id}/file", summary="预览PDF原文")
async def get_document_file(document_id: UUID, db: AsyncSession = Depends(get_db)):
    document = await db.get(Document, document_id)
    if not document:
        raise HTTPException(status_code=404, detail="文档不存在")
    try:
        content = await asyncio.to_thread(minio_client.download, document.file_path)
    except Exception as exc:
        logger.error("下载文档失败: %s", exc, exc_info=True)
        raise HTTPException(status_code=502, detail="文档存储服务暂时不可用") from exc
    return Response(
        content=content,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f"inline; filename*=UTF-8''{quote(document.file_name)}",
            "Cache-Control": "private, no-store",
        },
    )
