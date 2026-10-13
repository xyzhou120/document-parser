"""
关键词提取器（规则提取）
Phase 0硬编码5个字段
"""

import logging
import re
from typing import Dict, Optional

from app.config import settings
from app.models.contracts import Box, EvidenceRef, ExtractedField, Locator

logger = logging.getLogger(__name__)


class KeywordExtractor:
    """关键词规则提取器"""

    def __init__(self):
        # 从配置加载字段规则
        self.rules = {}
        fields_config = settings.fields.get("default", [])
        for field in fields_config:
            self.rules[field["field_key"]] = field

    async def extract(
        self, document_text: str, field_key: str, field_config: Optional[Dict] = None
    ) -> Optional[ExtractedField]:
        """
        提取单个字段

        Args:
            document_text: 文档全文
            field_key: 字段标识
            field_config: 字段配置（可选，如果提供则使用，否则使用默认规则）

        Returns:
            ExtractedField | None
        """
        # 获取规则
        rule = field_config or self.rules.get(field_key)
        if not rule:
            logger.warning(f"No rule found for field: {field_key}")
            return None

        # 提取pattern
        pattern = rule.get("pattern")
        if not pattern:
            logger.warning(f"No pattern defined for field: {field_key}")
            return None

        # 正则匹配
        try:
            match = re.search(pattern, document_text, re.MULTILINE)
        except re.error as e:
            logger.error(f"Invalid regex pattern for {field_key}: {e}")
            return None

        if not match:
            logger.debug(f"No match found for field: {field_key}")
            return None

        # 提取值
        value = match.group(1) if match.groups() else match.group(0)

        # 计算置信度（简单规则：匹配到就是高置信度）
        confidence = 0.95

        # 构造证据（简化版：不计算精确坐标）
        evidence = EvidenceRef(
            page_number=1,  # TODO: 精确定位到页面
            locator=Locator(
                coordinate_system="PDF_POINT",
                bbox=Box(x0=0, y0=0, x1=100, y1=20),  # TODO: 精确坐标
            ),
            quote=match.group(0),  # 匹配的原文
            confidence=confidence,
        )

        result = ExtractedField(
            field_key=field_key,
            field_value=value,
            extraction_method="keyword",
            confidence=confidence,
            evidence=evidence,
        )

        logger.info(f"Extracted field {field_key}: {value} (confidence: {confidence})")
        return result

    async def extract_all(
        self, document_text: str, field_keys: list[str]
    ) -> Dict[str, Optional[ExtractedField]]:
        """
        批量提取多个字段

        Args:
            document_text: 文档全文
            field_keys: 字段标识列表

        Returns:
            字段提取结果字典
        """
        results = {}
        for field_key in field_keys:
            results[field_key] = await self.extract(document_text, field_key)
        return results
