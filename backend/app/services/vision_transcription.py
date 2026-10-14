"""OCR 成功但没有文字时，用视觉模型转写原图。

空识别不是 OCR 服务故障。视觉模型只负责把图里的字读出来；它暂时不可用时，
同一次内容准备稍后重试，不计入 OCR 熔断。
"""

from __future__ import annotations

import base64
from collections.abc import Awaitable, Callable
from typing import Any

_PROMPT = (
    "只转写图片中可见的文字，保持阅读顺序和换行。不要解释，不要补写图片里没有的内容。"
    "如果图片中没有可辨认的文字，只回答 BLANK。"
)


def interpret_vision_text(text: str | None) -> dict[str, Any]:
    """把模型原文收成内容准备能保存的结果。BLANK 和空回答表示看过并且没有文字。"""
    body = str(text or "").strip()
    if not body or body.upper() == "BLANK":
        return {"state": "blank", "blank_verified": True, "source": "vision_fallback"}
    return {"text": body, "source": "vision_fallback"}


class VisionPageTranscriber:
    def __init__(self, complete: Callable[[bytes, int], Awaitable[str]] | None = None):
        self.complete = complete

    async def transcribe(self, image: bytes, page_number: int, region: dict | None = None) -> dict[str, Any]:
        del region
        if self.complete is not None:
            text = await self.complete(image, page_number)
        else:
            text = await _complete_with_settings(image, page_number)
        return interpret_vision_text(text)


async def _complete_with_settings(image: bytes, page_number: int) -> str:
    import httpx
    from openai import AsyncOpenAI

    from app.config import settings

    config = settings.models["vision_llm"]
    encoded = base64.b64encode(image).decode("ascii")
    client = AsyncOpenAI(
        base_url=config.base_url,
        api_key=config.api_key or "dummy",
        timeout=config.timeout,
        max_retries=0,
        http_client=httpx.AsyncClient(trust_env=config.trust_env),
    )
    try:
        response = await client.chat.completions.create(
            model=config.model_name,
            temperature=0,
            max_tokens=4000,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": f"第 {page_number} 页。{_PROMPT}"},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}},
                ],
            }],
        )
    finally:
        await client.close()
    message = response.choices[0].message.content if response.choices else ""
    return message or ""
