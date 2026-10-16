"""数据分析用的字段摘要。不保存证据原文。"""

_KEYS = (
    "contract_version",
    "semantic_state",
    "coverage_complete",
    "confidence",
    "coverage",
    "failures",
    "value_groups",
    "instances",
)
_NESTED = ("semantic_state", "coverage_complete")


def quality_inputs_from_detail(detail):
    if not isinstance(detail, dict) or not detail:
        return None
    result = {key: detail[key] for key in _KEYS if key in detail and detail[key] is not None}
    for name in ("field_completion", "completion"):
        nested = detail.get(name)
        if not isinstance(nested, dict):
            continue
        picked = {key: nested[key] for key in _NESTED if key in nested and nested[key] is not None}
        if picked:
            result[name] = picked
    return result or None
