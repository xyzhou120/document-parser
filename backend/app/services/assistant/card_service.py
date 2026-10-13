"""独立排队的文档卡片，仅基于绑定的来源摘录生成。"""

from __future__ import annotations

import re
from copy import deepcopy
from datetime import datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.models.assistant import DocumentCard
from app.models.content import (
    ContentInvalidation,
    ContentRevision,
    DocumentProcessingHead,
    IndexGeneration,
)
from app.models.database import Document
from app.models.execution import ExecutionOutbox
from app.services.content_service import ContentService
from app.services.execution.job_repository import JobRepository

OUTBOX_KIND = "assistant.card.build"


class CardUnavailable(ValueError):
    pass


def _id(value):
    return UUID(str(value)) if value is not None else None


def extract_card(content, revision_id, generation_id):
    """有来源支持的摘录和标题索引；不使用提取事实作为输入。"""
    headings, excerpts, citations = [], [], []
    for page in content.pages:
        blocks = page.get("text_blocks") or [{"text": page.get("text", ""), "block_id": None}]
        for block in blocks:
            raw = str(block.get("text") or "").strip()
            if not raw:
                continue
            cid = f"card-{len(citations) + 1}"
            quote = raw[:240]
            citation = {"citation_id": cid, "revision_id": str(revision_id), "generation_id": str(generation_id) if generation_id else None,
                        "block_id": block.get("block_id"), "page_number": page["page_number"], "bbox": deepcopy(block.get("bbox")),
                        "coordinate_system": block.get("coordinate_system"), "quote": quote, "source": block.get("source", "native")}
            heading = next((line.strip() for line in raw.splitlines() if re.match(r"^(?:第[一二三四五六七八九十百\d]+[章节]|[一二三四五六七八九十]+[、．.]|\d+(?:\.\d+)*[、．. ]|[（(][一二三四五六七八九十\d]+[）)])", line.strip()) and len(line.strip()) <= 100), None)
            used = False
            if heading and len(headings) < 20:
                # 标题文本原样保留，且必须包含完整引文。
                citation["quote"] = heading
                headings.append({"title": heading, "page_number": page["page_number"], "citation_ids": [cid]})
                used = True
            if len(excerpts) < 5:
                excerpts.append({"text": quote, "citation_ids": [cid]})
                if used and quote != heading:
                    citation["quote"] = quote
                    if heading not in quote:
                        headings.pop()
                used = True
            if used:
                citations.append(citation)
    topics = [{"text": item["title"], "citation_ids": item["citation_ids"]} for item in headings[:8]]
    risks = ["摘要为原文摘录，目录和主题按可识别标题展示"]
    if not content.coverage_complete:
        risks.append("资料卡仅覆盖当前已处理原文，不能代表附件全部内容")
    if not excerpts:
        risks.append("当前版本没有可引用的文字")
    return {"summary": "\n".join(item["text"] for item in excerpts),
            "outline": {"items": headings, "topics": topics, "summary_items": excerpts, "source": "source_excerpts"},
            "citations": citations, "risks": risks}


class CardService:
    def __init__(self, content=None):
        self.content = content or ContentService()

    async def _versions(self, db, document_id, revision_id=None, generation_id=None, *, lock=False):
        head = await db.get(DocumentProcessingHead, _id(document_id), with_for_update=lock, populate_existing=lock)
        revision_id = _id(revision_id) or (head.active_content_revision_id if head else None)
        if not revision_id:
            raise CardUnavailable("正文版本尚未就绪")
        revision = await db.get(ContentRevision, revision_id)
        if revision is None or revision.document_id != _id(document_id):
            raise CardUnavailable("正文版本不属于当前附件")
        generation_id = _id(generation_id) or (head.active_index_generation_id if head and head.active_content_revision_id == revision_id else None)
        if generation_id:
            generation = await db.get(IndexGeneration, generation_id)
            if generation is None or generation.revision_id != revision_id:
                raise CardUnavailable("索引与正文版本不一致")
        return head, revision_id, generation_id

    async def request(self, db, document_id, *, request_id, revision_id=None, generation_id=None):
        head, revision, generation = await self._versions(db, document_id, revision_id, generation_id, lock=True)
        if not head or head.active_content_revision_id != revision:
            raise CardUnavailable("请对当前正文版本生成资料卡")
        cid = uuid5(NAMESPACE_URL, f"document-card:{revision}:{generation}")
        dedupe = f"card-request:{document_id}:{request_id}"
        event = await db.scalar(select(ExecutionOutbox).where(ExecutionOutbox.kind == OUTBOX_KIND, ExecutionOutbox.dedupe_key == dedupe))
        if event and event.payload["card_id"] != str(cid):
            raise ValueError("request_id已绑定另一版本资料卡")
        await db.execute(insert(DocumentCard).values(card_id=cid, document_id=_id(document_id), revision_id=revision,
                         generation_id=generation, status="queued").on_conflict_do_nothing(index_elements=["card_id"]))
        card = await db.get(DocumentCard, cid)
        if event:
            return self.serialize(card)
        if card.status in {"failed", "invalidated"}:
            card.status, card.error = "queued", None
        if card.status == "queued":
            await JobRepository(db).enqueue_outbox(kind=OUTBOX_KIND, aggregate_id=cid, dedupe_key=dedupe,
                payload={"card_id": str(cid), "document_id": str(document_id), "revision_id": str(revision),
                         "generation_id": str(generation) if generation else None,
                         "invalidation_watermark": head.invalidation_watermark})
        await db.flush()
        return self.serialize(card)

    async def get(self, db, document_id, *, revision_id=None, generation_id=None):
        if await db.get(Document, _id(document_id)) is None:
            raise KeyError("附件不存在")
        try:
            head, revision, generation = await self._versions(db, document_id, revision_id, generation_id)
        except CardUnavailable:
            return {"document_id": str(document_id), "status": "unavailable", "card_id": None, "summary": None, "outline": {}, "topics": [], "citations": [], "risks": []}
        card = await db.scalar(select(DocumentCard).where(DocumentCard.revision_id == revision, DocumentCard.generation_id == generation))
        if card is None:
            return {"document_id": str(document_id), "status": "not_generated", "card_id": None,
                    "revision_id": str(revision), "generation_id": str(generation) if generation else None,
                    "summary": None, "outline": {}, "topics": [], "citations": [], "risks": []}
        response = self.serialize(card)
        invalidations = (await db.execute(select(ContentInvalidation).where(
            ContentInvalidation.document_id == _id(document_id), ContentInvalidation.effective_at <= datetime.utcnow()))).scalars().all()
        revoked = any((row.scope_selector or {}).get("scope", row.invalidation_kind) not in {"field", "fact", "parse_run", "generation"}
                      and str((row.scope_selector or {}).get("revision_id") or (row.scope_selector or {}).get("content_revision_id")) == str(revision)
                      for row in invalidations)
        if revoked:
            response.update(status="invalidated", summary=None, outline={}, topics=[], citations=[], risks=[*response["risks"], "资料卡原文证据已失效，请重新生成"])
            return response
        if not head or head.active_content_revision_id != revision:
            response.update(status="historical", risks=[*response["risks"], "资料卡属于历史正文版本"])
        return response

    @staticmethod
    def serialize(card):
        outline = deepcopy(card.outline or {})
        return {"card_id": str(card.card_id), "document_id": str(card.document_id), "revision_id": str(card.revision_id),
                "generation_id": str(card.generation_id) if card.generation_id else None, "status": card.status,
                "summary": card.summary, "outline": outline, "topics": outline.get("topics", []),
                "citations": deepcopy(card.citations or []), "risks": deepcopy(card.risks or []), "error": deepcopy(card.error)}

    async def build(self, sessions, card_id):
        try:
            async with sessions() as db:
                card = await db.get(DocumentCard, _id(card_id))
                if card is None:
                    raise KeyError("资料卡不存在")
                if card.status == "succeeded":
                    return self.serialize(card)
                read = await self.content.read_revision(db, card.revision_id)
                values = extract_card(read.content, card.revision_id, card.generation_id)
                document_id, revision_id = card.document_id, card.revision_id
                head = await db.get(DocumentProcessingHead, document_id)
                watermark = head.invalidation_watermark if head else None
            async with sessions() as db, db.begin():
                from app.services.content_publish_service import content_publish_service
                await content_publish_service.guard_revision(db, document_id=document_id, revision_id=revision_id,
                                                             expected_invalidation_watermark=watermark)
                card = await db.get(DocumentCard, _id(card_id), with_for_update=True, populate_existing=True)
                for key, value in values.items():
                    setattr(card, key, value)
                card.status, card.error = "succeeded", None
                return self.serialize(card)
        except Exception as exc:
            # 卡片失败不能写入会话、问答消息或解析运行。
            async with sessions() as db, db.begin():
                card = await db.get(DocumentCard, _id(card_id), with_for_update=True)
                if card and card.status != "succeeded":
                    card.status, card.error = "failed", {"code": "card_build_failed", "reason": type(exc).__name__}
                    return self.serialize(card)
                if card:
                    return self.serialize(card)
            raise


card_service = CardService()
