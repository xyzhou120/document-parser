"""提取策略基类与公共工具"""

import re
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any
from uuid import uuid4

from app.rules.spec import RuleSpec


@dataclass
class Instance:
    """一个提取实例

    对应设计文档中的 instances 结构：同一字段在文档中可能出现多处。
    """

    value: str | None
    page_num: int
    context: str = ""
    unit: str = ""
    confidence: float = 0.9
    source: str = ""
    # 证据位置。解析器能提供时必须提供；缺失时明确表示不可精确定位。
    occurrence_id: str = ""
    start_offset: int | None = None
    end_offset: int | None = None
    bbox: dict | None = None
    coordinate_system: str | None = None
    location_status: str = "unavailable"
    table_id: str | None = None
    table_title: str | None = None
    row_index: int | None = None
    column_index: int | None = None
    table_row: list[str] = field(default_factory=list)
    table_headers: list[str] = field(default_factory=list)
    dimensions: dict = field(default_factory=dict)
    methods: list[str] = field(default_factory=list)
    value_kind: str | None = None
    binding_status: str = "unknown"

    def __post_init__(self):
        if not self.occurrence_id:
            self.occurrence_id = str(uuid4())
        if not self.methods and self.source:
            self.methods = [self.source]

    def to_dict(self) -> dict:
        return asdict(self)


class Strategy(ABC):
    """提取策略基类"""

    name: str = "base"
    label: str = ""

    @abstractmethod
    async def run(self, rule: RuleSpec, pages: list[dict]) -> list[Instance]:
        """在页面上执行提取，返回实例列表"""

    # ---- 公共工具 ----

    @staticmethod
    def context_of(text: str, start: int, end: int, window: int = 50) -> str:
        """截取匹配位置前后 window 字符作为原文引用"""
        left = max(0, start - window)
        right = min(len(text), end + window)
        snippet = text[left:right]
        return re.sub(r"\s+", " ", snippet).strip()

    @staticmethod
    def value_pattern(rule: RuleSpec) -> str:
        """按数据类型推导通用取值正则

        字符串类型刻意收紧：只接受紧跟在分隔符（冒号/为/是）后的短片段，
        否则「煤矿名称：」这类关键词会把后面 60 字符内的任意一句话都当取值，
        在真实报告上会产生大量误提取。
        """
        data_type = (rule.data_type or "string").lower()
        if data_type in ("float", "number", "integer", "int"):
            return r"([-+]?\d+(?:\.\d+)?)"
        if data_type == "date":
            return r"(\d{4}\s*[-年/.]\s*\d{1,2}\s*[-月/.]\s*\d{1,2}\s*日?)"
        if data_type == "array":
            return r"([\s\S]{2,200})"
        # string：优先取分隔符之后的值，取不到再退化为紧邻的短词
        return (
            r"[:：为是]\s*([一-龥A-Za-z0-9（）()·、\-—_]{2,30}?)"
            r"(?=[，。,；;\s]|$)"
            r"|[:：为是]\s*([一-龥A-Za-z0-9（）()·、\-—_]{2,30})"
            r"|^([一-龥A-Za-z0-9（）()·、\-—_]{2,30})$"
        )

    @staticmethod
    def first_group(match: "re.Match") -> str:
        """取正则中第一个非空分组

        string 的取值正则含多个备选分支，命中哪个分支不确定，
        必须按分组取非空值，直接 group(1) 会拿到 None。
        """
        if match.groups():
            for group in match.groups():
                if group:
                    return str(group).strip()
        return str(match.group(0)).strip()

    @staticmethod
    def value_span(match: "re.Match") -> tuple[int, int]:
        for index, group in enumerate(match.groups(), 1):
            if group:
                return match.span(index)
        return match.span()

    @staticmethod
    def local_evidence(text: str, start: int, end: int) -> str:
        """用本句/本行限定维度绑定，不能跨工作面段落借用标签。"""
        left = max(text.rfind(delimiter, 0, start) for delimiter in ("\n", "。", "；", ";")) + 1
        stops = [
            index
            for delimiter in ("\n", "。", "；", ";")
            if (index := text.find(delimiter, end)) >= 0
        ]
        right = min(stops) if stops else len(text)
        return text[left:right]

    @staticmethod
    def normalize_number(value: str) -> str:
        """去掉千分位并归一化数值字符串"""
        cleaned = value.replace(",", "").replace("，", "").strip()
        try:
            number = float(cleaned)
        except (TypeError, ValueError):
            return str(value).strip()
        return str(int(number)) if number.is_integer() and "." not in cleaned else str(number)

    @staticmethod
    def dedupe(instances: list[Instance]) -> list[Instance]:
        """只合并同一位置的重复命中，不合并位置不同的相同事实。"""
        seen: set[tuple] = set()
        unique: list[Instance] = []
        for item in instances:
            key: tuple
            if item.start_offset is not None or item.end_offset is not None:
                key = (item.page_num, item.start_offset, item.end_offset, item.value)
            elif item.table_id is not None or item.row_index is not None:
                key = (item.page_num, item.table_id, item.row_index, item.column_index, item.value)
            else:
                if not item.bbox:
                    unique.append(item)
                    continue
                key = (item.page_num, tuple(sorted(item.bbox.items())), item.value)
            if key in seen:
                continue
            seen.add(key)
            unique.append(item)
        return unique

    @staticmethod
    def attach_evidence(item: Instance, page: dict, start: int, end: int) -> Instance:
        """页内偏移直接引用持久化文本；只有解析器已有坐标时才附坐标。"""
        item.start_offset, item.end_offset = start, end
        item.location_status = "text_offset"
        for block in page.get("text_blocks") or []:
            block_start = block.get("start_offset")
            block_end = block.get("end_offset")
            if block_start is None or block_end is None:
                continue
            if block_start <= start and end <= block_end and block.get("bbox"):
                bbox = block["bbox"]
                item.bbox = (
                    dict(zip(("x0", "y0", "x1", "y1"), bbox, strict=False))
                    if isinstance(bbox, list | tuple)
                    else bbox
                )
                item.location_status = "text_block"
                item.coordinate_system = block.get("coordinate_system") or "PDF_POINT"
                break
        return item

    @staticmethod
    def bind_dimensions(item: Instance, rule: RuleSpec, evidence: str) -> Instance:
        """仅在本地证据内匹配配置维度，不把整页其它工作面强绑给当前取值。"""
        required = getattr(rule, "dimensions", []) or []
        bindings = (rule.strategy or {}).get("dimension_bindings") or {}
        item.dimensions = {}
        for key in required:
            cfg = bindings.get(key) or {}
            pattern = cfg.get("regex") if isinstance(cfg, dict) else None
            if not pattern:
                continue
            try:
                matches = list(re.finditer(pattern, evidence))
            except re.error:
                continue
            # 多个不同维度标签时不猜所属对象。
            values = {Strategy.first_group(match) for match in matches}
            if len(values) == 1:
                match = matches[0]
                item.dimensions[key] = {"value": next(iter(values)), "evidence": match.group(0)}
        item.binding_status = (
            "bound"
            if required and all(k in item.dimensions for k in required)
            else ("not_required" if not required else "unknown")
        )
        # value_kind 必须被局部原文证明，规则要求本身不代表已证明取值角色。
        kind = getattr(rule, "value_kind", None)
        kind_cfg = (rule.strategy or {}).get("value_kind_binding") or {}
        if kind and any(
            str(anchor) in evidence for anchor in kind_cfg.get("keywords", []) if anchor
        ):
            item.value_kind = kind
        return item

    @staticmethod
    def extract_unit(value: str, default_unit: str | None) -> tuple[str, str]:
        """从取值中剥离单位，返回 (值, 单位)"""
        known_units = sorted(
            ["m³/min", "m3/min", "m³/t", "m3/t", "MPa", "m³", "m3", "个", "%", "m", "t", "℃"],
            key=len,
            reverse=True,
        )
        text = value.strip()
        for unit in known_units:
            if text.endswith(unit):
                return text[: -len(unit)].strip(), unit
        # 规则配置的 unit 是预期单位，不能冒充文档中实际出现的单位。
        return text, ""

    @staticmethod
    def unit_after(text: str) -> str:
        """只接受紧接取值的显式单位，长单位优先且不吞掉后续单位片段。"""
        match = re.match(
            r"\s*(m³/min|m3/min|m³/t|m3/t|MPa|m³|m3|个|%|m|t|℃)(?![A-Za-z0-9³/])", text
        )
        return match.group(1) if match else ""


def build_prompt(rule: RuleSpec, context: str) -> str:
    """构建 LLM 提取提示词

    规则自带 prompt_template 时以它为准，否则用字段信息生成通用提示词。
    """
    llm_cfg = (rule.strategy or {}).get("llm_extract") or {}
    template = llm_cfg.get("prompt_template")
    if template:
        return (
            str(template).replace("{{context}}", context).replace("{context}", context)
            + f"\n字段说明：{rule.field_description or rule.field_name_cn}"
            + f"\n对象维度：{rule.dimensions}；取值角色：{rule.value_kind or '未指定'}。"
            + "\n每处出现都返回，不要选择最佳值，不做达标或正确性判断。维度值需要同段原文或同表行证据。"
        )

    unit_hint = f"，单位 {rule.unit}" if rule.unit else ""
    return (
        f"你是煤矿文档信息提取专家。请从以下文档内容中提取「{rule.field_name_cn}」{unit_hint}。\n"
        f"字段说明：{getattr(rule, 'field_description', None) or rule.field_name_cn}\n"
        f"需要提取的对象维度：{getattr(rule, 'dimensions', []) or []}；"
        f"取值角色：{getattr(rule, 'value_kind', None) or '未指定'}。\n"
        "每处出现都返回，包括相同数值在不同对象/位置的出现；不要选择最佳值，不做达标或正确性判断。"
        "维度值只能来自同一段原文或同一表格行，缺失维度留空。\n\n"
        f"【文档内容】\n{context}\n\n"
        "【输出格式】严格返回 JSON，不要输出其他内容：\n"
        '{"instances": [{"value": "提取值", "page_num": 页码, "context": "含取值与对象标识的原文片段", "unit": "原文单位", "dimensions": {}, "confidence": 0.0}]}\n'
        '如果文档中不存在该字段，返回 {"instances": []}，不要虚构。'
    )


def parse_llm_instances(payload: str, rule: RuleSpec, fallback_page: int = 1) -> list[Instance]:
    """解析 LLM 返回的 JSON，尽力容错"""
    import json

    text = (payload or "").strip()
    if not text:
        return []

    # 去除可能的 Markdown 代码块围栏
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()

    data: Any = None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", text)
        if match:
            try:
                data = json.loads(match.group(0))
            except json.JSONDecodeError:
                data = None

    instances: list[Instance] = []
    if isinstance(data, dict):
        raw_items = data.get("instances")
        if raw_items is None and "value" in data:
            raw_items = [data]
        if isinstance(raw_items, list):
            for item in raw_items:
                if not isinstance(item, dict):
                    continue
                value = str(item.get("value", "")).strip()
                if not value or value.upper() in ("NOT_FOUND", "NONE", "NULL"):
                    continue
                try:
                    page = int(item.get("page_num") or fallback_page)
                except (TypeError, ValueError):
                    page = fallback_page
                try:
                    confidence = float(item.get("confidence", 0.85))
                except (TypeError, ValueError):
                    confidence = 0.85
                instances.append(
                    Instance(
                        value=value,
                        page_num=page,
                        context=str(item.get("context", "")).strip(),
                        unit=str(item.get("unit") or ""),
                        confidence=max(0.0, min(1.0, confidence)),
                        source="llm_extract",
                    )
                )
    # 顶层字符串数组缺少页码与原文证据，不能作为字段值接受。
    # 裸文本可能是网关错误、截断响应或模型拒绝；没有结构化 JSON 时不作为字段值。

    return instances
