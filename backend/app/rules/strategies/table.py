"""表格提取策略

优先使用 PyMuPDF 的表格识别能力；识别不到时退化为纯文本行的键值匹配。
"""

import logging
import re

from app.rules.spec import RuleSpec
from app.rules.strategies.base import Instance, Strategy

logger = logging.getLogger(__name__)


class TableStrategy(Strategy):
    """表格查找策略

    设计文档 table_lookup 的实现：
    - table_title_keywords 定位目标表格
    - header_keywords 定位目标列
    - row_position 决定取哪一行
    """

    name = "table_lookup"
    label = "表格查找"

    async def run(self, rule: RuleSpec, pages: list[dict]) -> list[Instance]:
        cfg = (rule.strategy or {}).get("table_lookup") or {}
        header_keywords = [k for k in (cfg.get("header_keywords") or []) if k]
        title_keywords = [k for k in (cfg.get("table_title_keywords") or []) if k]
        row_position = cfg.get("row_position") or "all_rows"

        results: list[Instance] = []
        for page in pages:
            for table_index, raw_table in enumerate(page.get("tables") or []):
                table = dict(raw_table)
                table.setdefault("table_id", f"page-{page['page_number']}-table-{table_index}")
                results.extend(
                    self._scan_table(
                        rule, page, table, header_keywords, title_keywords, row_position
                    )
                )

        if results:
            return self.dedupe(results)

        # 表格结构不可用时，退化为文本行的「表头: 值」匹配
        if title_keywords:
            return []
        return self.dedupe(self._fallback_text_scan(rule, pages, header_keywords))

    def _scan_table(
        self,
        rule: RuleSpec,
        page: dict,
        table: dict,
        header_keywords: list[str],
        title_keywords: list[str],
        row_position: str,
    ) -> list[Instance]:
        rows = table.get("rows") or []
        if not rows:
            return []

        # 若配置了表格标题关键词，要求页面文本中出现其一
        if title_keywords:
            title = table.get("title") or ""
            if not any(keyword in title for keyword in title_keywords):
                return []

        header_index = self._find_header_row(rows, header_keywords)
        if header_index is None:
            return []

        header = [self._clean_cell(cell) for cell in rows[header_index]]
        column_index = self._find_column(header, header_keywords)
        if column_index is None:
            return []
        data_rows = rows[header_index + 1 :]
        if not data_rows:
            return []

        if row_position == "all_rows":
            targets = list(enumerate(data_rows, start=1))
        elif row_position == "first_data_row":
            targets = [(1, data_rows[0])]
        else:
            # 未实现的行定位不能静默当作第一行，避免返回错误记录。
            return []

        results: list[Instance] = []
        for offset, row in targets:
            if not row:
                continue
            raw = self._cell_at(row, column_index, header, header_keywords)
            if not raw:
                continue
            value, unit = self.extract_unit(raw, rule.unit)
            if rule.data_type and rule.data_type.lower() in ("float", "integer", "number"):
                value = self.normalize_number(value)
            row_index = header_index + offset
            item = Instance(
                value=value,
                unit=unit or (rule.unit if rule.unit and rule.unit in header[column_index] else ""),
                page_num=page["page_number"],
                context=" | ".join(self._clean_cell(c) for c in row if c)[:200],
                confidence=0.97,
                source="table_lookup",
                table_id=str(table.get("table_id")),
                table_title=table.get("title"),
                row_index=row_index,
                column_index=column_index,
                table_row=[self._clean_cell(cell) for cell in row],
                table_headers=header,
                location_status="table_cell",
            )
            cells = table.get("cells") or []
            for cell in cells:
                if not isinstance(cell, dict):
                    continue
                if cell.get("row_index") == row_index and cell.get("column_index") == column_index:
                    bbox = cell.get("bbox")
                    if bbox:
                        item.bbox = (
                            dict(zip(("x0", "y0", "x1", "y1"), bbox, strict=False))
                            if isinstance(bbox, list | tuple)
                            else bbox
                        )
                        item.coordinate_system = (
                            cell.get("coordinate_system")
                            or table.get("coordinate_system")
                            or "PDF_POINT"
                        )
                    break
            # 表头列名与当前行共同组成局部证据，保留行内对象身份。
            self.bind_dimensions(item, rule, " | ".join(header) + "\n" + item.context)
            required = getattr(rule, "dimensions", []) or []
            for key in required:
                cfg = ((rule.strategy or {}).get("dimension_bindings") or {}).get(key) or {}
                aliases = cfg.get("column_keywords") or cfg.get("header_keywords") or []
                indices = [
                    index
                    for index, title in enumerate(header)
                    if any(alias in title for alias in aliases)
                ]
                if len(indices) == 1 and indices[0] < len(row):
                    dimension_value = self._clean_cell(row[indices[0]])
                    if dimension_value:
                        item.dimensions[key] = {
                            "value": dimension_value,
                            "evidence": f"{header[indices[0]]}: {dimension_value}",
                        }
            if required:
                item.binding_status = (
                    "bound" if all(key in item.dimensions for key in required) else "unknown"
                )
            results.append(item)
        return results

    def _fallback_text_scan(
        self, rule: RuleSpec, pages: list[dict], header_keywords: list[str]
    ) -> list[Instance]:
        """没有表格结构时，按行扫描「表头 值」形式"""
        keywords = header_keywords or list(
            (rule.strategy or {}).get("keyword_search", {}).get("keywords") or []
        )
        if not keywords:
            return []

        from app.rules.strategies.keyword import KeywordStrategy

        results: list[Instance] = []
        matcher = KeywordStrategy()
        keyword_cfg = (rule.strategy or {}).get("keyword_search") or {}
        for page in pages:
            text = page.get("text") or ""
            regex = keyword_cfg.get("regex")
            matches = (
                matcher._match_regex(rule, text, page, regex, 50)
                if regex
                else matcher._match_keywords(rule, text, page, keywords, 50)
            )
            for item in matches:
                item.source = "table_text"
                item.confidence = min(item.confidence, 0.85)
                results.append(item)
        return results

    @staticmethod
    def _clean_cell(cell) -> str:
        if cell is None:
            return ""
        return re.sub(r"\s+", " ", str(cell)).strip()

    def _find_header_row(self, rows: list[list], header_keywords: list[str]) -> int | None:
        """在前三行中寻找表头行"""
        if not header_keywords:
            return 0
        for index, row in enumerate(rows[:3]):
            cells = [self._clean_cell(c) for c in row]
            joined = " ".join(cells)
            if any(keyword in joined for keyword in header_keywords):
                return index
        return None

    def _find_column(self, header: list[str], header_keywords: list[str]) -> int | None:
        if not header_keywords:
            return None
        matches = [
            index
            for index, cell in enumerate(header)
            if any(keyword in cell for keyword in header_keywords)
        ]
        return matches[0] if len(matches) == 1 else None

    def _cell_at(
        self, row: list, column_index: int | None, header: list[str], header_keywords: list[str]
    ) -> str:
        """取出目标列的单元格；列下标未知时按位置启发式判断"""
        if column_index is not None and column_index < len(row):
            return self._clean_cell(row[column_index])

        return ""
