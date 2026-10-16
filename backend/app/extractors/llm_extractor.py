"""
LLM提取器（兜底）
使用大语言模型提取字段
"""

from app.services.model_capacity import limited_model_request

import logging
from typing import Dict, Optional

from openai import AsyncOpenAI

from app.config import settings
from app.models.contracts import ExtractedField

logger = logging.getLogger(__name__)


class LLMExtractor:
    """LLM提取器（兜底）"""

    def __init__(self):
        llm_config = settings.models.get("text_llm")
        if not llm_config:
            raise ValueError("text_llm configuration not found")

        self.client = AsyncOpenAI(
            base_url=llm_config.base_url, api_key=llm_config.api_key or "dummy"
        )
        self.model = llm_config.model_name
        self.timeout = llm_config.timeout

    async def extract(
        self, document_text: str, field_key: str, field_config: Dict
    ) -> Optional[ExtractedField]:
        """
        使用LLM提取字段

        Args:
            document_text: 文档全文（或相关片段）
            field_key: 字段标识
            field_config: 字段配置

        Returns:
            ExtractedField | None
        """
        # 构造Prompt
        prompt = self._build_prompt(document_text, field_config)

        try:
            # 调用LLM
            async with limited_model_request(self.client, self.model, timeout=self.timeout):
                response = await self.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": "你是一个专业的文档信息提取助手。"},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.1,
                    max_tokens=200,
                    timeout=self.timeout,
                )

            # 解析响应
            content = response.choices[0].message.content
            result = self._parse_response(content, field_key, field_config)

            if result:
                logger.info(
                    f"LLM extracted field {field_key}: {result.field_value} "
                    f"(confidence: {result.confidence})"
                )

            return result

        except Exception as e:
            logger.error(f"LLM extraction failed for {field_key}: {e}")
            return None

    def _build_prompt(self, document_text: str, field_config: Dict) -> str:
        """构造提取Prompt"""
        field_name = field_config.get("field_name", "未知字段")
        field_type = field_config.get("field_type", "STRING")
        unit = field_config.get("unit", "")

        # 限制文档长度（避免超长）
        max_length = 4000
        if len(document_text) > max_length:
            document_text = document_text[:max_length] + "..."

        prompt = f"""请从以下文档中提取"{field_name}"字段。

文档内容：
{document_text}

要求：
1. 只返回字段值，不要解释
2. 如果找不到，返回"NOT_FOUND"
3. 如果是数值，只返回数字，不要单位{f'（单位是{unit}）' if unit else ''}

请提取"{field_name}"："""

        return prompt

    def _parse_response(
        self, response: str, field_key: str, field_config: Dict
    ) -> Optional[ExtractedField]:
        """解析LLM响应"""
        response = response.strip()

        if response == "NOT_FOUND" or not response:
            return None

        # 简单处理：直接作为值
        return ExtractedField(
            field_key=field_key,
            field_value=response,
            extraction_method="llm",
            confidence=0.85,  # LLM置信度稍低
            evidence=None,  # LLM无法提供精确坐标
        )
