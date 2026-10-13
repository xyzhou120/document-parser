"""关键词与正则提取策略"""

import logging
import re

from app.rules.spec import RuleSpec
from app.rules.strategies.base import Instance, Strategy

logger = logging.getLogger(__name__)


class KeywordStrategy(Strategy):
    """关键词定位 + 上下文取值

    设计文档 keyword_search 的实现：
    - keywords 定位字段出现位置
    - regex 优先，命中即取值
    - 无 regex 时按数据类型推导通用取值模式，在关键词后的上下文窗口内取值
    """

    name = "keyword_search"
    label = "关键词检索"

    async def run(self, rule: RuleSpec, pages: list[dict]) -> list[Instance]:
        cfg = (rule.strategy or {}).get("keyword_search") or {}
        keywords = [k for k in (cfg.get("keywords") or []) if k]
        regex = cfg.get("regex")
        window = int(cfg.get("context_window") or 50)
        regions = cfg.get("search_regions") or ["all"]

        instances: list[Instance] = []
        for page in self._select_pages(pages, regions):
            text = page.get("text") or ""
            if not text:
                continue

            if regex:
                instances.extend(self._match_regex(rule, text, page, regex, window))

            # 配置了显式 regex 时，regex 是该路径的唯一取值定义；
            # 不再在 regex 失败后用泛化关键词扫描窃取邻近数字。
            if keywords and not regex:
                instances.extend(self._match_keywords(rule, text, page, keywords, window))

        if not instances and keywords:
            logger.debug("keyword strategy found nothing for %s", rule.field_key)
        return self.dedupe(instances)

    def _match_regex(
        self, rule: RuleSpec, text: str, page: dict, regex: str, window: int
    ) -> list[Instance]:
        results: list[Instance] = []
        try:
            compiled = re.compile(regex, re.MULTILINE)
        except re.error as exc:
            logger.warning("invalid regex for %s: %s", rule.field_key, exc)
            return results

        for match in compiled.finditer(text):
            raw = self.first_group(match)
            if not raw:
                continue
            value, unit = self.extract_unit(raw, rule.unit)
            if rule.data_type and rule.data_type.lower() in ("float", "integer", "number"):
                value = self.normalize_number(value)
            start, end = self.value_span(match)
            item = Instance(
                value=value,
                unit=unit,
                page_num=page["page_number"],
                context=self.context_of(text, match.start(), match.end(), window),
                confidence=0.95,
                source="keyword_search",
            )
            self.attach_evidence(item, page, start, end)
            item.unit = item.unit or self.unit_after(text[end:])
            self.bind_dimensions(item, rule, self.local_evidence(text, start, end))
            results.append(item)
        return results

    def _match_keywords(
        self, rule: RuleSpec, text: str, page: dict, keywords: list[str], window: int
    ) -> list[Instance]:
        results: list[Instance] = []
        pattern = self.value_pattern(rule)
        try:
            compiled = re.compile(pattern)
        except re.error:
            return results

        for keyword in keywords:
            for match in re.finditer(re.escape(keyword), text):
                tail = text[match.end() : match.end() + max(1, window)]
                # 值必须紧邻字段标签，并通过明确分隔符连接；避免把后续年份/其它指标当作当前字段。
                if tail and not re.match(r"\s*[:：=为是]\s*", tail):
                    continue
                if not tail:
                    continue
                numeric = (rule.data_type or "").lower() in ("float", "integer", "number", "int")
                tail_offset = match.end()
                if numeric:
                    separator = re.match(r"^\s*[:：=为是]\s*", tail)
                    if separator is None:
                        continue
                    tail_offset += separator.end()
                    tail = tail[separator.end() :]
                value_match = compiled.match(tail) if numeric else compiled.search(tail)
                if not value_match:
                    continue
                raw = self.first_group(value_match)
                if not raw:
                    continue
                value, unit = self.extract_unit(raw, rule.unit)
                if rule.data_type and rule.data_type.lower() in ("float", "integer", "number"):
                    value = self.normalize_number(value)
                local_start, local_end = self.value_span(value_match)
                start, end = tail_offset + local_start, tail_offset + local_end
                item = Instance(
                    value=value,
                    unit=unit,
                    page_num=page["page_number"],
                    context=self.context_of(text, match.start(), match.end(), window),
                    confidence=0.92,
                    source="keyword_search",
                )
                self.attach_evidence(item, page, start, end)
                item.unit = item.unit or self.unit_after(text[end:])
                self.bind_dimensions(item, rule, self.local_evidence(text, start, end))
                results.append(item)
        return results

    @staticmethod
    def _select_pages(pages: list[dict], regions: list[str]) -> list[dict]:
        """按 search_regions 限定检索范围"""
        if not regions or "all" in regions:
            return pages
        selected: list[dict] = []
        total = len(pages)
        for region in regions:
            if region == "last_5_pages":
                selected.extend(pages[-5:])
            elif region == "first_5_pages":
                selected.extend(pages[:5])
            elif region == "last_page":
                selected.extend(pages[-1:])
            elif region == "first_page":
                selected.extend(pages[:1])
        if not selected:
            return pages
        # 去重且保持页码顺序
        seen: set[int] = set()
        ordered: list[dict] = []
        for page in selected:
            number = page["page_number"]
            if number in seen:
                continue
            seen.add(number)
            ordered.append(page)
        return ordered if len(ordered) <= total else pages


class RegexStrategy(Strategy):
    """纯正则策略"""

    name = "regex"
    label = "正则匹配"

    async def run(self, rule: RuleSpec, pages: list[dict]) -> list[Instance]:
        cfg = (rule.strategy or {}).get("regex") or {}
        if isinstance(cfg, str):
            cfg = {"pattern": cfg}
        pattern = cfg.get("pattern") or (rule.strategy or {}).get("keyword_search", {}).get("regex")
        if not pattern:
            return []

        try:
            compiled = re.compile(pattern, re.MULTILINE)
        except re.error as exc:
            logger.warning("invalid regex strategy for %s: %s", rule.field_key, exc)
            return []

        window = int(cfg.get("context_window") or 50)
        results: list[Instance] = []
        for page in pages:
            text = page.get("text") or ""
            for match in compiled.finditer(text):
                raw = self.first_group(match)
                if not raw:
                    continue
                value, unit = self.extract_unit(raw, rule.unit)
                if rule.data_type and rule.data_type.lower() in ("float", "integer", "number"):
                    value = self.normalize_number(value)
                start, end = self.value_span(match)
                item = Instance(
                    value=value,
                    unit=unit,
                    page_num=page["page_number"],
                    context=self.context_of(text, match.start(), match.end(), window),
                    confidence=0.9,
                    source="regex",
                )
                self.attach_evidence(item, page, start, end)
                item.unit = item.unit or self.unit_after(text[end:])
                self.bind_dimensions(item, rule, self.local_evidence(text, start, end))
                results.append(item)
        return self.dedupe(results)
