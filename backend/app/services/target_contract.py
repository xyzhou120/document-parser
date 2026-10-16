"""受控目标结果的共享结构和轻量归一化。

模型输出仍然走事实验证器，但 statement/table 目标不能被压扁成普通
字符串。这个模块只负责把模型的扩展字段归一到稳定的结果载荷，不作
业务合格判断。
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

TARGET_TYPES = {"fact", "statement", "table"}
STRUCTURED_TARGET_TYPES = {"statement", "table"}
TARGET_STATUSES = {
    "NOT_RUN", "RUNNING", "FAILED", "NOT_FOUND_AFTER_COMPLETE_SEARCH",
    "PARTIAL", "REVIEW_REQUIRED", "DELIVERABLE",
}
STATEMENT_POLARITIES = {"affirmed", "negated", "uncertain", "unknown"}
_STATE_TO_STATUS = {
    "passed": "DELIVERABLE",
    "completed": "DELIVERABLE",
    "not_found": "NOT_FOUND_AFTER_COMPLETE_SEARCH",
    "missing": "NOT_FOUND_AFTER_COMPLETE_SEARCH",
    "incomplete": "PARTIAL",
    "partial_candidates": "PARTIAL",
    "review_required": "REVIEW_REQUIRED",
    "failed": "FAILED",
    "running": "RUNNING",
}


def structured_target_rejection(rules, engine: str, strategy: str | None) -> str | None:
    """原文和表格只走可靠提取的页批策略。其他入口使用同一判断。"""
    if not any(target_type(rule) in STRUCTURED_TARGET_TYPES for rule in rules):
        return None
    if engine == "reliable" and strategy == "page_batch_v1":
        return None
    return "原文和表格目标需要可靠提取的 page_batch_v1 策略"


def target_status_for_state(state: str | None) -> str:
    """执行语义状态到对外目标状态。不根据有没有值推断可交付。"""
    return _STATE_TO_STATUS.get(state or "", "REVIEW_REQUIRED")


def instance_target_status(item: Mapping, field_status: str | None) -> str:
    row_status = str(item.get("row_status") or "")
    if row_status in TARGET_STATUSES:
        return row_status
    status = str(field_status or "")
    return status if status in TARGET_STATUSES else "NOT_RUN"


def target_type(rule: Any) -> str:
    value = rule.get("target_type") if isinstance(rule, Mapping) else getattr(rule, "target_type", None)
    return str(value or "fact")


def normalize_decision(decision: Mapping, rule: Any) -> dict:
    """将 statement/table 扩展输出转换为验证器可消费的标量入口。"""
    result = dict(decision)
    kind = target_type(rule)
    if kind == "statement":
        statement = result.get("statement") or result.get("statement_payload") or {}
        if isinstance(statement, str):
            statement = {"text": statement}
        statement = dict(statement) if isinstance(statement, Mapping) else {}
        text = statement.get("text") or result.get("quote") or result.get("value")
        if text is not None:
            result["value"] = str(text)
            result["quote"] = str(statement.get("quote") or result.get("quote") or text)
        polarity = str(statement.get("polarity") or result.get("polarity") or "unknown")
        result["statement"] = {
            "text": str(text or ""),
            "polarity": polarity if polarity in STATEMENT_POLARITIES else "unknown",
            "subject": statement.get("subject"),
            "qualifiers": deepcopy(statement.get("qualifiers") or {}),
        }
    elif kind == "table":
        row = result.get("table_row") or result.get("row") or {}
        if isinstance(row, Mapping):
            row = dict(row)
            cells = row.get("cells") or {}
            if isinstance(cells, list):
                cells = {str(cell.get("column_key") or cell.get("column") or index): cell
                         for index, cell in enumerate(cells) if isinstance(cell, Mapping)}
            row["cells"] = cells if isinstance(cells, Mapping) else {}
            result["table_row"] = row
            # 当前执行器按一个单元格事实做证据校验；保留行载荷供交付层聚合。
            if not result.get("value"):
                first = next((cell for cell in row["cells"].values() if isinstance(cell, Mapping)), None)
                if first is not None:
                    result["value"] = first.get("value")
                    result["unit"] = result.get("unit") or first.get("unit")
                    result["table_relation"] = result.get("table_relation") or first.get("table_relation")
            if row.get("row_key") is not None:
                result["table_row_key"] = str(row["row_key"])
            elif row.get("identity"):
                result["table_row_key"] = "|".join(f"{key}={value}" for key, value in sorted(row["identity"].items()))
    return result


def result_payload(kind: str, item: Mapping[str, Any]) -> dict:
    """返回对外交付的类型化载荷，保留兼容的 value 字段。"""
    if kind == "statement":
        statement = deepcopy(item.get("statement") or {})
        text = str(statement.get("text") or item.get("value") or item.get("quote") or "")
        statement["text"] = text
        statement.setdefault("polarity", "unknown")
        statement.setdefault("quote", item.get("quote") or text)
        statement["evidence_ids"] = list(item.get("evidence_ids") or statement.get("evidence_ids") or [])
        return {"type": "statement", "statement": statement}
    if kind == "table":
        row = deepcopy(item.get("table_row") or {})
        row.setdefault("row_key", item.get("table_row_key"))
        row.setdefault("identity", deepcopy(item.get("dimensions") or {}))
        row.setdefault("cells", {})
        if not row["cells"] and item.get("value") is not None:
            relation = deepcopy(item.get("table_relation") or {})
            column_key = str(relation.get("column_key") or relation.get("column_header") or "value")
            row["cells"] = {column_key: {"value": item.get("value"), "unit": item.get("unit"),
                                          "table_relation": relation, "evidence_ids": list(item.get("evidence_ids") or [])}}
        row["status"] = item.get("row_status") or row.get("status") or "REVIEW_REQUIRED"
        return {"type": "table", "row": row}
    return {"type": "fact", "value": item.get("value"), "unit": item.get("unit"),
            "dimensions": deepcopy(item.get("dimensions") or {})}


def attach_target_payload(fact: dict, decision: Mapping, rule: Any) -> dict:
    """把目标专属结构写入事实实例，兼容旧的 value/quote 字段。"""
    kind = target_type(rule)
    if kind == "statement":
        statement = normalize_decision(decision, rule).get("statement") or {}
        fact["statement"] = deepcopy(statement)
        fact["target_payload"] = result_payload(kind, {**fact, "statement": statement})
        fact["raw_value"] = statement.get("text") or fact.get("value")
    elif kind == "table":
        normalized = normalize_decision(decision, rule)
        row = normalized.get("table_row") or {}
        if row:
            fact["table_row"] = deepcopy(row)
            for cell in fact["table_row"].get("cells", {}).values():
                if isinstance(cell, dict):
                    cell["evidence_ids"] = list(fact.get("evidence_ids") or [])
        fact["table_row_key"] = normalized.get("table_row_key") or row.get("row_key")
        # 执行器已经判定待复核时，不采用模型自报的可交付。
        if fact.get("row_status") == "REVIEW_REQUIRED":
            fact["row_status"] = "REVIEW_REQUIRED"
        else:
            fact["row_status"] = row.get("status") or "REVIEW_REQUIRED"
        fact["target_payload"] = result_payload(kind, {**fact, **normalized})
    return fact
