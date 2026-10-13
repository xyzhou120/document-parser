"""提取策略注册表"""

from app.rules.strategies.base import Instance, Strategy, build_prompt, parse_llm_instances
from app.rules.strategies.keyword import KeywordStrategy, RegexStrategy
from app.rules.strategies.llm import LLMStrategy
from app.rules.strategies.table import TableStrategy

# 方法名 -> 策略实例。新增策略只需在此注册。
_REGISTRY: dict[str, Strategy] = {
    KeywordStrategy.name: KeywordStrategy(),
    RegexStrategy.name: RegexStrategy(),
    TableStrategy.name: TableStrategy(),
    # LLM 策略持有客户端，复用同一实例
    LLMStrategy.name: LLMStrategy(),
}

# 兼容设计文档中的别名写法
_ALIASES: dict[str, str] = {
    "keyword": "keyword_search",
    "keywords": "keyword_search",
    "table": "table_lookup",
    "llm": "llm_extract",
    "llm_extract": "llm_extract",
    # OCR/vision 尚无独立字段级策略，禁止静默伪装成文本 LLM。
}

METHOD_LABELS: dict[str, str] = {
    "keyword_search": "关键词检索",
    "table_lookup": "表格查找",
    "llm_extract": "模型提取",
    "regex": "正则匹配",
}


def resolve_method(method: str) -> str | None:
    """把方法名或别名解析为注册表中的标准名"""
    if not method:
        return None
    if method in _REGISTRY:
        return method
    return _ALIASES.get(method.lower())


def get_strategy(method: str) -> Strategy | None:
    """按方法名取策略实例"""
    resolved = resolve_method(method)
    return _REGISTRY.get(resolved) if resolved else None


def available_methods() -> list[dict]:
    """给前端规则编辑器用的可用方法清单"""
    return [
        *({"value": name, "label": METHOD_LABELS.get(name, name)} for name in _REGISTRY),
        {"value": "agent_extract", "label": "清单知识包智能体（由解析引擎执行）"},
    ]


__all__ = [
    "Instance",
    "Strategy",
    "METHOD_LABELS",
    "available_methods",
    "build_prompt",
    "get_strategy",
    "parse_llm_instances",
    "resolve_method",
]
