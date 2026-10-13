"""规则种子数据

首次启动（或显式调用）时把三类清单的示例规则写入数据库，
让平台开箱即可配置、即可验证。

规则内容对应 cc组方案/详细设计/1-具体清单规则配置示例.md。
"""

import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.rules import ChecklistType, ExtractionRule, RuleChangeLog
from app.rules.agent_knowledge import CHECKLIST as AGENT_CHECKLIST
from app.rules.agent_knowledge import agent_seed_rules

logger = logging.getLogger(__name__)


CHECKLIST_TYPES = [
    {
        "checklist_type": "regional_prediction",
        "checklist_name": "区域预测临界值考察报告",
        "description": "用于确定防突措施临界值的考察报告，关注残余瓦斯含量、瓦斯压力等临界值。",
        "sort_order": 10,
    },
    {
        "checklist_type": "gas_drainage_standard",
        "checklist_name": "瓦斯抽采达标评判报告",
        "description": "抽采达标评判，关注钻孔设计/实际数量、测点数量与可解吸瓦斯含量。",
        "sort_order": 20,
    },
    {
        "checklist_type": "borehole_ledger",
        "checklist_name": "钻孔竣工验收台账",
        "description": "钻孔竣工验收明细台账，主要为钻孔明细列表。",
        "sort_order": 30,
    },
]


RULES = [
    # ---- 区域预测临界值考察报告 ----
    {
        "checklist_type": "regional_prediction",
        "field_key": "residual_gas_threshold",
        "field_name_cn": "残余瓦斯含量临界值",
        "field_description": "煤层残余瓦斯含量临界值，用于判断防突措施效果；未标注时按国家标准 8 m³/t。",
        "data_type": "float",
        "unit": "m³/t",
        "required": True,
        "multi_instance": True,
        "merge_strategy": "value_consistent",
        "priority": 100,
        "fallback_value": "8.0",
        "strategy": {
            "methods": ["table_lookup", "keyword_search", "llm_extract"],
            "preferred_source": "table",
            "confidence_threshold": 0.8,
            "table_lookup": {
                "table_title_keywords": ["临界值", "考察结果", "确定值"],
                "header_keywords": ["残余瓦斯", "临界值", "瓦斯含量"],
                "row_position": "first_data_row",
            },
            "keyword_search": {
                "keywords": ["残余瓦斯含量临界值", "瓦斯含量临界值", "临界值"],
                "regex": "残余瓦斯含量临界值[为是]?\\s*[:：]?\\s*(\\d+(?:\\.\\d+)?)\\s*m³/t",
                "context_window": 50,
                "search_regions": ["all"],
            },
            "llm_extract": {
                "context_scope": "keyword_window",
                "temperature": 0.1,
                "prompt_template": (
                    "你是煤矿文档信息提取专家。请从以下文档内容中提取「残余瓦斯含量临界值」。\n"
                    "【背景知识】\n"
                    "- 该值通常在《区域预测临界值考察报告》中明确标注\n"
                    "- 常见表述：经考察确定，残余瓦斯含量临界值为 8.5 m³/t\n"
                    "- 若有多处出现，优先选择「经考察确定」「最终确定」「审批确定」等权威表述\n"
                    "- 若未找到，返回空列表，不要虚构\n\n"
                    "【文档内容】\n{context}\n\n"
                    "【输出格式】严格返回 JSON:\n"
                    '{"instances": [{"value": "仅数字", "page_num": 页码, '
                    '"context": "包含该值的原文30-50字", "confidence": 0.0}]}\n'
                    '未找到时返回 {"instances": []}'
                ),
            },
        },
        "validation": {"min": 0, "max": 50, "typical_range": [5, 15]},
    },
    {
        "checklist_type": "regional_prediction",
        "field_key": "gas_pressure_threshold",
        "field_name_cn": "瓦斯压力临界值",
        "field_description": "煤层瓦斯压力临界值，常与残余瓦斯含量在同一行出现。",
        "data_type": "float",
        "unit": "MPa",
        "required": True,
        "multi_instance": True,
        "merge_strategy": "value_consistent",
        "priority": 90,
        "fallback_value": "0.74",
        "strategy": {
            "methods": ["table_lookup", "keyword_search", "llm_extract"],
            "preferred_source": "table",
            "table_lookup": {
                "table_title_keywords": ["临界值", "考察结果"],
                "header_keywords": ["瓦斯压力", "压力临界值"],
                "row_position": "first_data_row",
            },
            "keyword_search": {
                "keywords": ["瓦斯压力临界值", "压力临界值"],
                "regex": "瓦斯压力临界值[为是]?\\s*[:：]?\\s*(\\d+(?:\\.\\d+)?)\\s*MPa",
                "context_window": 50,
            },
            "llm_extract": {
                "context_scope": "keyword_window",
                "temperature": 0.1,
                "prompt_template": (
                    "从以下文档内容中提取「瓦斯压力临界值」，单位 MPa。\n"
                    "【文档内容】\n{context}\n\n"
                    '严格返回 JSON: {"instances": [{"value": "数字", "page_num": 页码, "confidence": 0.0}]}'
                ),
            },
        },
        "validation": {"min": 0, "max": 5, "typical_range": [0.5, 1.5]},
    },
    {
        "checklist_type": "regional_prediction",
        "field_key": "report_date",
        "field_name_cn": "报告编制时间",
        "field_description": "报告的编制或出具日期。",
        "data_type": "date",
        "unit": None,
        "required": True,
        "multi_instance": False,
        "priority": 60,
        "strategy": {
            "methods": ["keyword_search", "llm_extract"],
            "keyword_search": {
                "keywords": ["报告编制时间", "编制日期", "报告日期", "编制时间"],
                "regex": "(?:报告编制时间|编制日期|报告日期)[:：]?\\s*(\\d{4}\\s*[-年/.]\\s*\\d{1,2}\\s*[-月/.]\\s*\\d{1,2}\\s*日?)",
                "context_window": 30,
            },
            "llm_extract": {
                "context_scope": "keyword_window",
                "temperature": 0.1,
            },
        },
    },
    {
        "checklist_type": "regional_prediction",
        "field_key": "reviewer_signature",
        "field_name_cn": "复核人签字",
        "field_description": "报告复核人姓名，通常出现在签字页或审批表。",
        "data_type": "string",
        "unit": None,
        "required": False,
        "multi_instance": False,
        "priority": 40,
        "strategy": {
            "methods": ["keyword_search", "llm_extract"],
            "keyword_search": {
                "keywords": ["复核人", "复核", "审核人"],
                "regex": "(?:复核人|复核|审核人)[:：]?\\s*([\\u4e00-\\u9fa5]{2,4})",
                "context_window": 30,
                "search_regions": ["all"],
            },
            "llm_extract": {
                "context_scope": "keyword_window",
                "temperature": 0.1,
                "prompt_template": (
                    "从以下文档内容中提取复核人姓名（2-4 个汉字）。\n"
                    "【文档内容】\n{context}\n\n"
                    '严格返回 JSON: {"instances": [{"value": "姓名", "page_num": 页码, "confidence": 0.0}]}\n'
                    '未找到返回 {"instances": []}'
                ),
            },
        },
        "validation": {"regex": "^[\\u4e00-\\u9fa5]{2,4}$", "blacklist": ["无", "空", "未填"]},
    },
    {
        "checklist_type": "regional_prediction",
        "field_key": "coal_seam_name",
        "field_name_cn": "考察煤层名称",
        "field_description": "被考察煤层的名称，如「3号煤层」。",
        "data_type": "string",
        "unit": None,
        "required": True,
        "multi_instance": True,
        "merge_strategy": "keep_all",
        "priority": 70,
        "strategy": {
            "methods": ["keyword_search", "llm_extract"],
            "keyword_search": {
                "keywords": ["考察煤层", "煤层名称", "煤层"],
                "regex": "(?:考察煤层|煤层名称|煤层)[:：]?\\s*([0-9一二三四五六七八九十]+\\s*号?\\s*煤层)",
                "context_window": 40,
            },
            "llm_extract": {
                "context_scope": "keyword_window",
                "temperature": 0.1,
            },
        },
    },
    # ---- 瓦斯抽采达标评判报告 ----
    {
        "checklist_type": "gas_drainage_standard",
        "field_key": "mine_name",
        "field_name_cn": "煤矿名称",
        "field_description": "上报文档所属煤矿名称。",
        "data_type": "string",
        "unit": None,
        "required": True,
        "multi_instance": False,
        "priority": 100,
        "strategy": {
            "methods": ["keyword_search", "llm_extract"],
            "keyword_search": {
                "keywords": ["煤矿名称", "矿井名称", "煤矿"],
                "regex": "(?:煤矿名称|矿井名称)[:：]?\\s*([\\u4e00-\\u9fa5A-Za-z0-9]{2,30}(?:煤矿|矿井|煤业))",
                "context_window": 40,
            },
            "llm_extract": {
                "context_scope": "keyword_window",
                "temperature": 0.1,
            },
        },
    },
    {
        "checklist_type": "gas_drainage_standard",
        "field_key": "designed_boreholes",
        "field_name_cn": "钻孔设计数量",
        "field_description": "设计钻孔数量，单位：个。",
        "data_type": "integer",
        "unit": "个",
        "required": True,
        "multi_instance": False,
        "priority": 95,
        "strategy": {
            "methods": ["table_lookup", "keyword_search", "llm_extract"],
            "table_lookup": {
                "header_keywords": ["设计", "设计孔数", "设计钻孔"],
                "row_position": "first_data_row",
            },
            "keyword_search": {
                "keywords": ["钻孔设计数量", "设计孔数", "设计钻孔"],
                "regex": "(?:钻孔设计数量|设计孔数|设计钻孔)[^0-9]{0,8}(\\d+)\\s*个?",
                "context_window": 40,
            },
            "llm_extract": {"context_scope": "keyword_window", "temperature": 0.1},
        },
        "validation": {"min": 0, "max": 100000},
    },
    {
        "checklist_type": "gas_drainage_standard",
        "field_key": "actual_boreholes",
        "field_name_cn": "钻孔实际数量",
        "field_description": "实际施工钻孔数量，单位：个。",
        "data_type": "integer",
        "unit": "个",
        "required": True,
        "multi_instance": False,
        "priority": 90,
        "strategy": {
            "methods": ["table_lookup", "keyword_search", "llm_extract"],
            "table_lookup": {
                "header_keywords": ["实际", "实际孔数", "实际钻孔"],
                "row_position": "first_data_row",
            },
            "keyword_search": {
                "keywords": ["钻孔实际数量", "实际孔数", "实际钻孔", "钻孔"],
                "regex": "(?:钻孔实际数量|实际孔数|实际钻孔|钻孔)[^0-9]{0,8}(\\d+)\\s*个",
                "context_window": 40,
            },
            "llm_extract": {"context_scope": "keyword_window", "temperature": 0.1},
        },
        "validation": {"min": 0, "max": 100000},
    },
    {
        "checklist_type": "gas_drainage_standard",
        "field_key": "measuring_points",
        "field_name_cn": "测点数量",
        "field_description": "抽采达标的测点数量。",
        "data_type": "integer",
        "unit": "个",
        "required": True,
        "multi_instance": False,
        "priority": 80,
        "strategy": {
            "methods": ["table_lookup", "keyword_search", "llm_extract"],
            "table_lookup": {
                "header_keywords": ["测点", "测点数"],
                "row_position": "first_data_row",
            },
            "keyword_search": {
                "keywords": ["测点数量", "测点数", "测点"],
                "regex": "(?:测点数量|测点数|测点)[^0-9]{0,8}(\\d+)\\s*个",
                "context_window": 40,
            },
            "llm_extract": {"context_scope": "keyword_window", "temperature": 0.1},
        },
        "validation": {"min": 0, "max": 100000},
    },
    {
        "checklist_type": "gas_drainage_standard",
        "field_key": "measured_gas_content",
        "field_name_cn": "可解吸瓦斯含量实测值",
        "field_description": "实测可解吸瓦斯含量，单位 m³/t。",
        "data_type": "float",
        "unit": "m³/t",
        "required": True,
        "multi_instance": True,
        "merge_strategy": "keep_all",
        "priority": 70,
        "strategy": {
            "methods": ["table_lookup", "keyword_search", "llm_extract"],
            "table_lookup": {
                "header_keywords": ["可解吸瓦斯含量", "解吸瓦斯", "实测"],
                "row_position": "first_data_row",
            },
            "keyword_search": {
                "keywords": ["可解吸瓦斯含量", "解吸瓦斯含量"],
                "regex": "可解吸瓦斯含量[^0-9]{0,10}(\\d+(?:\\.\\d+)?)\\s*m³/t",
                "context_window": 50,
            },
            "llm_extract": {"context_scope": "keyword_window", "temperature": 0.1},
        },
        "validation": {"min": 0, "max": 50, "typical_range": [1, 15]},
    },
    {
        "checklist_type": "gas_drainage_standard",
        "field_key": "drainage_volume",
        "field_name_cn": "瓦斯抽采量",
        "field_description": "累计瓦斯抽采量，单位 m³。",
        "data_type": "float",
        "unit": "m³",
        "required": False,
        "multi_instance": False,
        "priority": 60,
        "strategy": {
            "methods": ["keyword_search", "llm_extract"],
            "keyword_search": {
                "keywords": ["抽采量", "抽采总量", "瓦斯抽采量"],
                "regex": "抽采量[为是]?\\s*[:：]?\\s*(\\d+(?:\\.\\d+)?)\\s*m³",
                "context_window": 40,
            },
            "llm_extract": {"context_scope": "keyword_window", "temperature": 0.1},
        },
        "validation": {"min": 0},
    },
    {
        "checklist_type": "gas_drainage_standard",
        "field_key": "report_date",
        "field_name_cn": "报告日期",
        "field_description": "报告的编制日期。",
        "data_type": "date",
        "unit": None,
        "required": True,
        "multi_instance": False,
        "priority": 50,
        "strategy": {
            "methods": ["keyword_search", "llm_extract"],
            "keyword_search": {
                "keywords": ["报告日期", "编制日期", "日期"],
                "regex": "(?:报告日期|编制日期)[:：]?\\s*(\\d{4}\\s*[-年/.]\\s*\\d{1,2}\\s*[-月/.]\\s*\\d{1,2}\\s*日?)",
                "context_window": 30,
            },
            "llm_extract": {"context_scope": "keyword_window", "temperature": 0.1},
        },
    },
    # ---- 钻孔竣工验收台账 ----
    {
        "checklist_type": "borehole_ledger",
        "field_key": "borehole_details",
        "field_name_cn": "钻孔明细列表",
        "field_description": "台账中的钻孔明细，通常为多行表格数据。",
        "data_type": "array",
        "unit": None,
        "required": True,
        "multi_instance": True,
        "merge_strategy": "keep_all",
        "priority": 100,
        "strategy": {
            "methods": ["table_lookup", "llm_extract"],
            "table_lookup": {
                "header_keywords": ["孔号", "钻孔编号", "进尺", "孔深"],
                "row_position": "all_rows",
            },
            "llm_extract": {
                "context_scope": "keyword_window",
                "temperature": 0.1,
                "prompt_template": (
                    "从以下台账内容中提取钻孔明细摘要（孔号与总数、合格数）。\n"
                    "【文档内容】\n{context}\n\n"
                    '严格返回 JSON: {"instances": [{"value": "总孔数N，合格M", "page_num": 页码, "confidence": 0.0}]}'
                ),
            },
        },
    },
    {
        "checklist_type": "borehole_ledger",
        "field_key": "total_boreholes",
        "field_name_cn": "钻孔总数",
        "field_description": "台账内钻孔总数量。",
        "data_type": "integer",
        "unit": "个",
        "required": True,
        "multi_instance": False,
        "priority": 90,
        "strategy": {
            "methods": ["keyword_search", "table_lookup", "llm_extract"],
            "keyword_search": {
                "keywords": ["钻孔总数", "总孔数", "合计"],
                "regex": "(?:钻孔总数|总孔数|合计)[^0-9]{0,8}(\\d+)\\s*个?",
                "context_window": 40,
            },
            "llm_extract": {"context_scope": "keyword_window", "temperature": 0.1},
        },
        "validation": {"min": 0, "max": 100000},
    },
]

# 独立的试点包保留现有配置规则和历史运行记录。
CHECKLIST_TYPES.append(AGENT_CHECKLIST)
RULES.extend(agent_seed_rules())


async def seed_rules(db: AsyncSession, force: bool = False) -> dict:
    """写入种子规则

    Args:
        db: 数据库会话
        force: 为 True 时覆盖已存在的同类规则

    Returns:
        统计信息
    """
    created_types, created_rules, skipped = 0, 0, 0

    history = (await db.execute(select(RuleChangeLog))).scalars().all()
    deleted_types = {
        entry.checklist_type for entry in history if entry.action == "checklist_type_delete"
    }
    deleted_rules = {
        (entry.checklist_type, (entry.before_state or {}).get("field_key"))
        for entry in history
        if entry.action == "delete"
    }

    for item in CHECKLIST_TYPES:
        if not force and item["checklist_type"] in deleted_types:
            continue
        existing = await db.get(ChecklistType, item["checklist_type"])
        if existing is None:
            db.add(ChecklistType(**item))
            created_types += 1
        else:
            # 已存在时不覆盖人工修改过的名称与说明
            created_types += 0
    await db.flush()

    existing_counts = {}
    for item in CHECKLIST_TYPES:
        existing_counts[item["checklist_type"]] = int(
            await db.scalar(
                select(func.count(ExtractionRule.rule_id)).where(
                    ExtractionRule.checklist_type == item["checklist_type"]
                )
            )
            or 0
        )

    for item in RULES:
        if not force and (
            item["checklist_type"] in deleted_types
            or (item["checklist_type"], item["field_key"]) in deleted_rules
        ):
            skipped += 1
            continue
        result = await db.execute(
            select(ExtractionRule).where(
                ExtractionRule.checklist_type == item["checklist_type"],
                ExtractionRule.field_key == item["field_key"],
            )
        )
        existing = result.scalar_one_or_none()
        # 启动种子写入是幂等的：清单拥有用户管理的规则后，删除种子规则的操作
        # 在重启后仍必须生效。
        if existing is None and not force and existing_counts.get(item["checklist_type"], 0) > 0:
            skipped += 1
            continue
        if existing is not None and not force:
            skipped += 1
            continue
        if existing is not None and force:
            before = {
                key: getattr(existing, key)
                for key in item
                if key not in ("checklist_type", "field_key")
            }
            for key, value in item.items():
                if key not in ("checklist_type", "field_key"):
                    setattr(existing, key, value)
            existing.version = (existing.version or 1) + 1
            db.add(
                RuleChangeLog(
                    rule_id=existing.rule_id,
                    checklist_type=existing.checklist_type,
                    action="seed_force_update",
                    before_state=before,
                    after_state=dict(item),
                    operator="system",
                )
            )
        else:
            entity = ExtractionRule(**item)
            db.add(entity)
            await db.flush()
            db.add(
                RuleChangeLog(
                    rule_id=entity.rule_id,
                    checklist_type=entity.checklist_type,
                    action="seed_create",
                    before_state=None,
                    after_state=dict(item),
                    operator="system",
                )
            )
        created_rules += 1

    await db.commit()
    from app.rules.repository import rule_repository

    rule_repository.invalidate_cache()

    total = await db.scalar(select(func.count(ExtractionRule.rule_id))) or 0
    logger.info(
        "规则种子完成：清单 %d 类，新增规则 %d 条，跳过 %d 条，当前共 %d 条",
        created_types,
        created_rules,
        skipped,
        total,
    )
    return {
        "checklist_types": created_types,
        "rules_created": created_rules,
        "rules_skipped": skipped,
        "rules_total": total,
    }
