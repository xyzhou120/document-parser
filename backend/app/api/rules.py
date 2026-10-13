"""规则管理 API

清单类型与提取规则的增删改查、启停、批量导入、变更历史。
"""

import csv
import io
import json
import logging
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.rules import ChecklistType, ExtractionRule
from app.rules.repository import rule_repository
from app.rules.seeding import seed_rules
from app.rules.strategies import available_methods
from app.schemas.rules import (
    ChangeLogResponse,
    ChecklistTypePatch,
    ChecklistTypePayload,
    ChecklistTypeResponse,
    RuleImportPayload,
    RulePatch,
    RulePayload,
    RuleResponse,
    RuleTogglePayload,
)
from app.storage import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/rules", tags=["规则管理"])


# ============ 元数据 ============


@router.get("/meta", summary="规则编辑器元数据")
async def get_meta():
    """前端规则编辑器需要的枚举与选项"""
    return {
        "data_types": [
            {"value": "string", "label": "字符串"},
            {"value": "integer", "label": "整数"},
            {"value": "float", "label": "浮点数"},
            {"value": "date", "label": "日期"},
            {"value": "array", "label": "数组/明细"},
            {"value": "json", "label": "JSON"},
        ],
        "methods": available_methods(),
        "merge_strategies": [
            {"value": "value_consistent", "label": "历史配置（执行时保留全部实例）"},
            {"value": "keep_all", "label": "保留全部实例"},
            {"value": "most_recent", "label": "历史配置（不再按页码裁决）"},
        ],
        "search_regions": [
            {"value": "all", "label": "全文"},
            {"value": "first_page", "label": "首页"},
            {"value": "last_page", "label": "末页"},
            {"value": "first_5_pages", "label": "前 5 页"},
            {"value": "last_5_pages", "label": "后 5 页"},
        ],
    }


@router.post("/seed", summary="写入/重置示例规则")
async def seed(db: AsyncSession = Depends(get_db), force: bool = Query(False)):
    """把三类清单的示例规则写入数据库

    force=true 时用种子内容覆盖已存在的同名规则。
    """
    result = await seed_rules(db, force=force)
    return {"message": "示例规则已写入", **result}


# ============ 清单类型 ============


@router.get("/checklist-types", response_model=list[ChecklistTypeResponse], summary="清单类型列表")
async def list_checklist_types(
    only_active: bool = Query(False), db: AsyncSession = Depends(get_db)
):
    return await rule_repository.list_checklist_types(db, only_active=only_active)


@router.post("/checklist-types", response_model=ChecklistTypeResponse, summary="新建/更新清单类型")
async def create_checklist_type(payload: ChecklistTypePayload, db: AsyncSession = Depends(get_db)):
    entity = await rule_repository.upsert_checklist_type(db, payload.model_dump())
    counts = await rule_repository._rule_counts(db)
    return ChecklistTypeResponse(
        checklist_type=entity.checklist_type,
        checklist_name=entity.checklist_name,
        description=entity.description,
        is_active=entity.is_active,
        sort_order=entity.sort_order,
        rule_count=counts.get(entity.checklist_type, 0),
        created_at=entity.created_at,
        updated_at=entity.updated_at,
    )


@router.patch(
    "/checklist-types/{checklist_type}",
    response_model=ChecklistTypeResponse,
    summary="修改清单类型",
)
async def patch_checklist_type(
    checklist_type: str, payload: ChecklistTypePatch, db: AsyncSession = Depends(get_db)
):
    entity = await db.get(ChecklistType, checklist_type)
    if entity is None:
        raise HTTPException(status_code=404, detail="清单类型不存在")

    data = payload.model_dump(exclude_unset=True)
    if any(k in data and data[k] is None for k in ("checklist_name", "is_active", "sort_order")):
        raise HTTPException(status_code=422, detail="清单名称、状态和排序不能为 null")
    updated = await rule_repository.upsert_checklist_type(
        db, {"checklist_type": checklist_type, **data}
    )
    counts = await rule_repository._rule_counts(db)
    return ChecklistTypeResponse(
        checklist_type=updated.checklist_type,
        checklist_name=updated.checklist_name,
        description=updated.description,
        is_active=updated.is_active,
        sort_order=updated.sort_order,
        rule_count=counts.get(updated.checklist_type, 0),
        created_at=updated.created_at,
        updated_at=updated.updated_at,
    )


@router.delete("/checklist-types/{checklist_type}", summary="删除清单类型")
async def delete_checklist_type(checklist_type: str, db: AsyncSession = Depends(get_db)):
    ok = await rule_repository.delete_checklist_type(db, checklist_type)
    if not ok:
        raise HTTPException(status_code=404, detail="清单类型不存在")
    return {"message": f"清单 {checklist_type} 及其规则已删除"}


# ============ 规则 ============


@router.get("", response_model=list[RuleResponse], summary="规则列表")
async def list_rules(
    checklist_type: str | None = Query(None),
    enabled: bool | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    only_enabled = enabled is True
    rules = await rule_repository.list_rules(
        db, checklist_type=checklist_type, only_enabled=only_enabled
    )
    if enabled is False:
        rules = [r for r in rules if not r.enabled]
    return [RuleResponse(**rule_repository.rule_to_dict(r)) for r in rules]


@router.post("", response_model=RuleResponse, summary="新建规则")
async def create_rule(payload: RulePayload, db: AsyncSession = Depends(get_db)):
    duplicate = await db.execute(
        select(ExtractionRule).where(
            ExtractionRule.checklist_type == payload.checklist_type,
            ExtractionRule.field_key == payload.field_key,
        )
    )
    if duplicate.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="该清单中已存在相同的字段标识")

    rule = await rule_repository.create_rule(db, payload.model_dump())
    return RuleResponse(**rule_repository.rule_to_dict(rule))


@router.get("/{rule_id:uuid}", response_model=RuleResponse, summary="规则详情")
async def get_rule(rule_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    rule = await rule_repository.get_rule(db, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="规则不存在")
    return RuleResponse(**rule_repository.rule_to_dict(rule))


@router.patch("/{rule_id:uuid}", response_model=RuleResponse, summary="修改规则")
async def patch_rule(rule_id: uuid.UUID, payload: RulePatch, db: AsyncSession = Depends(get_db)):
    data = payload.model_dump(exclude_unset=True)
    if not data:
        raise HTTPException(status_code=400, detail="没有需要更新的字段")

    rule = await rule_repository.get_rule(db, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="规则不存在")

    # A partial patch must also validate the resulting knowledge/dimension contract.
    try:
        RulePayload.model_validate({**rule_repository.rule_to_dict(rule), **data})
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # 字段标识变更时检查冲突
    new_key = data.get("field_key")
    if new_key and new_key != rule.field_key:
        conflict = await db.execute(
            select(ExtractionRule).where(
                ExtractionRule.checklist_type == rule.checklist_type,
                ExtractionRule.field_key == new_key,
                ExtractionRule.rule_id != rule_id,
            )
        )
        if conflict.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="该清单中已存在相同的字段标识")

    updated = await rule_repository.update_rule(db, rule_id, data)
    return RuleResponse(**rule_repository.rule_to_dict(updated))


@router.delete("/{rule_id:uuid}", summary="删除规则")
async def delete_rule(rule_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    ok = await rule_repository.delete_rule(db, rule_id)
    if not ok:
        raise HTTPException(status_code=404, detail="规则不存在")
    return {"message": "规则已删除"}


@router.post("/{rule_id:uuid}/toggle", response_model=RuleResponse, summary="启用/停用规则")
async def toggle_rule(
    rule_id: uuid.UUID, payload: RuleTogglePayload, db: AsyncSession = Depends(get_db)
):
    rule = await rule_repository.toggle_rule(db, rule_id, payload.enabled)
    if rule is None:
        raise HTTPException(status_code=404, detail="规则不存在")
    return RuleResponse(**rule_repository.rule_to_dict(rule))


# ============ 批量导入 ============


@router.post("/import", summary="批量导入规则（JSON）")
async def import_rules(payload: RuleImportPayload, db: AsyncSession = Depends(get_db)):
    if not payload.rules:
        raise HTTPException(status_code=400, detail="导入内容为空")
    entity = await db.get(ChecklistType, payload.checklist_type)
    if entity is None:
        raise HTTPException(status_code=404, detail="清单类型不存在，请先创建清单")
    result = await rule_repository.bulk_import(db, payload.checklist_type, payload.rules)
    return {"message": "导入完成", **result}


@router.post("/import-file", summary="从 CSV/JSON 文件导入规则")
async def import_rules_file(
    checklist_type: str = Form(...),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
):
    """支持 CSV（由 Excel 直接另存）与 JSON 两种格式"""
    entity = await db.get(ChecklistType, checklist_type)
    if entity is None:
        raise HTTPException(status_code=404, detail="清单类型不存在，请先创建清单")

    max_bytes = 5 * 1024 * 1024
    raw = await file.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise HTTPException(status_code=413, detail="规则文件不能超过 5MB")
    if not raw:
        raise HTTPException(status_code=400, detail="文件内容为空")

    filename = (file.filename or "").lower()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = raw.decode("gbk")
        except UnicodeDecodeError:
            raise HTTPException(status_code=400, detail="文件编码需为 UTF-8 或 GBK") from None

    rules: list[dict] = []
    if filename.endswith(".json"):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail=f"JSON 解析失败: {exc}") from exc
        rules = payload.get("rules") if isinstance(payload, dict) else payload
        if not isinstance(rules, list):
            raise HTTPException(status_code=400, detail="JSON 需为规则数组或包含 rules 字段")
    else:
        reader = csv.DictReader(io.StringIO(text))
        for row in reader:
            rules.append(_row_to_rule(row))

    if len(rules) > 1000:
        raise HTTPException(status_code=413, detail="单次最多导入 1000 条规则")
    if not rules:
        raise HTTPException(status_code=400, detail="未解析到任何规则")

    result = await rule_repository.bulk_import(db, checklist_type, rules)
    return {"message": "导入完成", **result}


# CSV 表头 -> 内部字段名的映射，兼容中英文两种表头
_HEADER_MAP = {
    "字段标识": "field_key",
    "field_key": "field_key",
    "字段名称": "field_name_cn",
    "field_name_cn": "field_name_cn",
    "数据类型": "data_type",
    "data_type": "data_type",
    "单位": "unit",
    "unit": "unit",
    "是否必填": "required",
    "required": "required",
    "提取方法": "methods",
    "methods": "methods",
    "关键词": "keywords",
    "keywords": "keywords",
    "正则": "regex",
    "regex": "regex",
    "LLM提示词": "prompt_template",
    "prompt_template": "prompt_template",
    "验证规则": "validation",
    "validation": "validation",
    "优先级": "priority",
    "priority": "priority",
    "兜底值": "fallback_value",
    "fallback_value": "fallback_value",
}


def _row_to_rule(row: dict) -> dict:
    """把一行 CSV 转成规则字典"""
    mapped: dict[str, str] = {}
    for header, value in row.items():
        if header is None:
            continue
        key = _HEADER_MAP.get(str(header).strip())
        if key:
            mapped[key] = (value or "").strip()

    rule: dict = {
        "field_key": mapped.get("field_key", ""),
        "field_name_cn": mapped.get("field_name_cn") or mapped.get("field_key", ""),
        "data_type": mapped.get("data_type") or "string",
        "unit": mapped.get("unit") or None,
        "required": mapped.get("required", "").lower() in ("是", "true", "1", "yes"),
        "priority": int(mapped["priority"]) if mapped.get("priority", "").isdigit() else 50,
        "enabled": True,
    }
    if mapped.get("fallback_value"):
        rule["fallback_value"] = mapped["fallback_value"]

    methods = [
        m.strip() for m in (mapped.get("methods") or "").replace("，", ",").split(",") if m.strip()
    ] or ["keyword_search"]
    strategy: dict = {"methods": methods}

    if mapped.get("keywords"):
        strategy["keyword_search"] = {
            "keywords": [
                k.strip()
                for k in mapped["keywords"].replace("，", ";").replace(",", ";").split(";")
                if k.strip()
            ],
            "context_window": 50,
        }
    if mapped.get("regex"):
        strategy.setdefault("keyword_search", {})["regex"] = mapped["regex"]
    if mapped.get("prompt_template"):
        strategy["llm_extract"] = {
            "prompt_template": mapped["prompt_template"],
            "temperature": 0.1,
        }
    rule["strategy"] = strategy

    if mapped.get("validation"):
        validation: dict = {}
        for item in mapped["validation"].replace("，", ",").split(","):
            if ":" not in item:
                continue
            key, value = item.split(":", 1)
            key, value = key.strip(), value.strip()
            if key in ("min", "max"):
                try:
                    validation[key] = float(value)
                except ValueError:
                    continue
            else:
                validation[key] = value
        if validation:
            rule["validation"] = validation

    return rule


# ============ 变更历史 ============


@router.get("/change-log", response_model=list[ChangeLogResponse], summary="规则变更历史")
async def change_log(
    checklist_type: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    logs = await rule_repository.list_change_log(db, checklist_type=checklist_type, limit=limit)
    return [
        ChangeLogResponse(
            log_id=log.log_id,
            rule_id=log.rule_id,
            checklist_type=log.checklist_type,
            action=log.action,
            before_state=log.before_state,
            after_state=log.after_state,
            operator=log.operator,
            created_at=log.created_at,
        )
        for log in logs
    ]
