"""LLM 提取策略"""

import logging
import re
from dataclasses import replace

from app.config import settings
from app.rules.spec import RuleSpec
from app.rules.strategies.base import (
    Instance,
    Strategy,
    build_prompt,
    parse_llm_instances,
)

logger = logging.getLogger(__name__)


class LLMStrategy(Strategy):
    """按规则提示词调用大模型提取

    与旧 LLMExtractor 的区别：
    - 提示词来自规则配置，而不是按字段名拼装
    - 只把与关键词相关的上下文（或全文前若干字符）送进模型，控制时延
    - 支持返回多实例
    """

    name = "llm_extract"
    label = "模型提取"

    # 单次调用送入模型的最大上下文字符数
    MAX_CONTEXT = 6000
    # 关键词窗口检索时，每处匹配取多少字符
    WINDOW = 600

    @staticmethod
    def _normalize_base(raw: str) -> str:
        """归一化 OpenAI 兼容端点

        openai SDK 会把 base_url 当作根路径、只追加 /chat/completions，
        因此配置若只有 host:port 会打到网关首页并收到 HTML。
        这里统一补上 /v1。
        """
        url = (raw or "").strip().rstrip("/")
        if not url:
            return url
        if not url.startswith(("http://", "https://")):
            url = f"http://{url}"
        if not url.endswith("/v1"):
            url = f"{url}/v1"
        return url

    def _get_client(self):
        # Celery 每次任务创建新的事件循环；HTTP 客户端不能跨 asyncio.run 复用。
        from openai import AsyncOpenAI

        llm_config = settings.models.get("text_llm")
        if not llm_config:
            raise ValueError("text_llm 配置缺失")
        return AsyncOpenAI(
            base_url=self._normalize_base(llm_config.base_url),
            api_key=llm_config.api_key or "dummy",
        )

    async def run(self, rule: RuleSpec, pages: list[dict]) -> list[Instance]:
        cfg = (rule.strategy or {}).get("llm_extract") or {}
        context, page_hint = self._build_context(rule, pages, cfg)
        if not context.strip():
            return []

        prompt = build_prompt(rule, context)
        prompt += "\n每个实例必须包含该取值所在的真实原文 context 和页码。找不到原文就返回空列表。"
        try:
            content = await self._chat(prompt, cfg)
        except Exception as exc:  # 外部服务异常不应中断整条链路
            logger.error("LLM 提取失败 %s: %s", rule.field_key, exc)
            return []

        instances = parse_llm_instances(content, rule, fallback_page=page_hint)
        if not instances:
            return []

        # 用上下文原文校正页码，尽量给出可核验的来源
        verified = []
        for item in instances:
            if not item.context or not item.value:
                continue
            # 原文引用和取值必须能在同一页核验，禁止模型幻觉成为高置信度字段。
            page_num = self._resolve_page(pages, item, page_hint)
            if not page_num:
                continue
            item.page_num = page_num
            item.confidence = min(item.confidence, 0.85)
            page = next(page for page in pages if page["page_number"] == page_num)
            # 在原始页文本中重新定位 quote 与 value，不能采用模型自报坐标或维度。
            text = page.get("text") or ""
            quote_pattern = r"\s*".join(
                re.escape(char) for char in re.sub(r"\s+", "", item.context)
            )
            for quote_match in re.finditer(quote_pattern, text):
                value_pattern = re.escape(item.value)
                if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", item.value):
                    value_pattern = rf"(?<![\d.]){value_pattern}(?![\d.])"
                for value_match in re.finditer(value_pattern, quote_match.group(0)):
                    grounded = replace(
                        item, occurrence_id="", dimensions={}, methods=["llm_extract"]
                    )
                    start = quote_match.start() + value_match.start()
                    end = quote_match.start() + value_match.end()
                    self.attach_evidence(grounded, page, start, end)
                    self.bind_dimensions(grounded, rule, self.local_evidence(text, start, end))
                    if grounded.unit and grounded.unit not in quote_match.group(0):
                        grounded.unit = ""
                    verified.append(grounded)
        return self.dedupe(verified)

    async def _chat(self, prompt: str, cfg: dict | None = None) -> str:
        client = self._get_client()
        runtime = cfg or getattr(self, "_llm_runtime", {}) or {}
        llm_config = settings.models["text_llm"]
        try:
            from app.services.model_capacity import limited_model_request
            async with limited_model_request(client, llm_config.model_name, timeout=llm_config.timeout):
                response = await client.chat.completions.create(
                    model=llm_config.model_name,
                    messages=[
                        {"role": "system", "content": "你是煤矿文档信息提取专家，只输出 JSON。"},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=float(runtime.get("temperature", 0.1)),
                    max_tokens=int(runtime.get("max_tokens", 1500)),
                    timeout=llm_config.timeout,
                )
        finally:
            await client.close()

        # 网关异常时会返回 HTML 或字符串而非标准对象，这里显式处理，
        # 避免抛出 "'str' object has no attribute 'choices'" 这类难以定位的错误。
        if isinstance(response, str):
            snippet = response.strip()[:120].replace("\n", " ")
            raise RuntimeError(
                f"模型服务返回了非 JSON 内容（{snippet}），请检查 base_url 是否指向 OpenAI 兼容端点"
            )

        choices = getattr(response, "choices", None)
        if not choices:
            raise RuntimeError(f"模型服务未返回 choices 字段: {type(response).__name__}")
        return choices[0].message.content or ""

    def _build_context(self, rule: RuleSpec, pages: list[dict], cfg: dict) -> tuple[str, int]:
        """按 context_scope 构建送入模型的上下文"""
        scope = cfg.get("context_scope") or "keyword_window"
        keywords = (rule.strategy or {}).get("keyword_search", {}).get("keywords") or []

        if scope == "full_document":
            text = "\n\n".join(f"【第{p['page_number']}页】\n{p.get('text') or ''}" for p in pages)
            return text[: self.MAX_CONTEXT], pages[0]["page_number"] if pages else 1

        if scope == "full_document" or not keywords:
            if not keywords:
                text = "\n\n".join(
                    f"【第{p['page_number']}页】\n{p.get('text') or ''}" for p in pages
                )
                return text[: self.MAX_CONTEXT], pages[0]["page_number"] if pages else 1

        # 关键词窗口：只送相关片段，显著降低时延
        chunks: list[str] = []
        first_page = pages[0]["page_number"] if pages else 1
        budget = self.MAX_CONTEXT
        for page in pages:
            text = page.get("text") or ""
            for keyword in keywords:
                start = 0
                while True:
                    index = text.find(keyword, start)
                    if index < 0:
                        break
                    left = max(0, index - self.WINDOW // 2)
                    right = min(len(text), index + self.WINDOW)
                    snippet = text[left:right].strip()
                    if snippet and snippet not in chunks:
                        chunks.append(f"【第{page['page_number']}页】\n{snippet}")
                        if len(chunks) == 1:
                            first_page = page["page_number"]
                    start = index + len(keyword)
                    if sum(len(c) for c in chunks) >= budget:
                        break
                if sum(len(c) for c in chunks) >= budget:
                    break
            if sum(len(c) for c in chunks) >= budget:
                break

        if not chunks:
            text = "\n\n".join(f"【第{p['page_number']}页】\n{p.get('text') or ''}" for p in pages)
            return text[: self.MAX_CONTEXT], first_page

        joined = "\n\n---\n\n".join(chunks)
        return joined[:budget], first_page

    @staticmethod
    def _resolve_page(pages: list[dict], instance: Instance, default: int) -> int:
        """用原文片段在页面中定位真实页码"""

        def normalize(text):
            return re.sub(r"\s+", "", text or "")

        value = normalize(instance.value)
        if not value:
            return 0
        pattern = re.escape(value)
        if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", value):
            pattern = rf"(?<![\d.]){pattern}(?![\d.])"
        matches = []
        for page in pages:
            text = normalize(page.get("text"))
            quote = normalize(instance.context)
            if quote and quote not in text:
                continue
            if re.search(pattern, quote or text):
                matches.append(page["page_number"])
        if instance.page_num in matches:
            return instance.page_num
        return matches[0] if len(matches) == 1 else 0

    @staticmethod
    def _page_context(pages: list[dict], page_num: int, value: str) -> str:
        for page in pages:
            if page["page_number"] != page_num:
                continue
            text = page.get("text") or ""
            index = text.find(value)
            if index >= 0:
                left = max(0, index - 50)
                right = min(len(text), index + len(value) + 50)
                return text[left:right].replace("\n", " ").strip()
        return ""
