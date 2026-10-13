"""Append-only offline error samples, separate from source/fact adjudication."""

from copy import deepcopy
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.models.assistant import QAMessage, QASession
from app.models.assistant_feedback import AssistantFeedbackSample
from app.models.content import ContentRevision, IndexGeneration
from app.models.database import ExtractedFieldModel, ParseRun
from app.services.assistant.index_repository import digest
from app.services.content_service import ContentService


class FeedbackService:
    def __init__(self, content=None):
        self.content = content or ContentService()

    async def submit(self, db, message_id, payload):
        message = await db.get(QAMessage, UUID(str(message_id)))
        if message is None:
            raise KeyError("助手消息不存在")
        session = await db.get(QASession, message.session_id)
        revision = await db.get(ContentRevision, message.revision_id)
        if session is None or revision is None or revision.document_id != session.document_id:
            raise ValueError("消息来源与附件版本不一致")
        read = await self.content.read_revision(db, message.revision_id)
        run = None
        field = None
        if payload.parse_run_id:
            run = await db.get(ParseRun, payload.parse_run_id)
            if run is None or run.document_id != session.document_id or run.content_revision_id != message.revision_id:
                raise ValueError("反馈运行与消息正文版本不一致")
            if payload.field_key:
                field = await db.scalar(select(ExtractedFieldModel).where(
                    ExtractedFieldModel.parse_run_id == run.parse_run_id, ExtractedFieldModel.field_key == payload.field_key))
                if field is None:
                    raise ValueError("反馈字段结果不存在")
        generation = await db.get(IndexGeneration, message.generation_id) if message.generation_id else None
        if generation and generation.revision_id != message.revision_id:
            raise ValueError("反馈索引版本不一致")
        locator = payload.locator.model_dump(mode="json") if payload.locator else None
        if locator:
            page = next((p for p in read.content.pages if p["page_number"] == locator["page_number"]), None)
            if page is None:
                raise ValueError("反馈定位不属于该正文版本")
            if locator.get("block_id"):
                block = next((b for b in page.get("text_blocks") or [] if b["block_id"] == locator["block_id"]), None)
                if block is None or locator.get("quote") and locator["quote"] not in block["text"]:
                    raise ValueError("反馈原文块或引用不匹配")
                if locator.get("bbox") is not None and locator["bbox"] != block.get("bbox"):
                    raise ValueError("反馈坐标与固定原文块不一致")
            elif locator.get("quote") and locator["quote"] not in page.get("text", ""):
                raise ValueError("反馈引用不在固定原文中")
            if locator.get("citation_id"):
                citation = next((c for c in message.citations or [] if c.get("citation_id") == locator["citation_id"]), None)
                if citation is None or citation.get("page_number") != locator["page_number"]:
                    raise ValueError("反馈引用ID不属于该消息")
        image_hashes = set()
        for page in read.content.pages:
            for item in [page, *(page.get("text_blocks") or []), *(page.get("regions") or [])]:
                observed = item.get("image_hash") or (item.get("image_ref") or {}).get("image_hash")
                if observed:
                    image_hashes.add(str(observed))
        if payload.image_hash and payload.image_hash not in image_hashes:
            raise ValueError("反馈图像hash未绑定此正文版本")
        rule = next((r for r in (run.extraction_profile or {}).get("rules", []) if r.get("field_key") == payload.field_key), None) if run else None
        original = {"question": message.question, "answer": message.answer, "claims": deepcopy(message.claims or []),
                    "citations": deepcopy(message.citations or []), "execution_status": message.execution_state}
        if field:
            original["field"] = {"field_key": field.field_key, "field_value": field.field_value,
                                 "instances": deepcopy(field.instances or []), "extraction_detail": deepcopy(field.extraction_detail or {}),
                                 "validation_status": field.validation_status, "source": "automatic"}
        snapshot = {
            "document_id": str(session.document_id), "file_hash": revision.file_hash,
            "content_revision_id": str(message.revision_id), "content_digest": revision.content_digest,
            "generation_id": str(message.generation_id) if message.generation_id else None,
            "index_snapshot_hash": generation.snapshot_hash if generation else None,
            "qa_message_id": str(message.message_id), "parse_run_id": str(run.parse_run_id) if run else None,
            "rule": deepcopy(rule), "rule_hash": digest(rule) if rule else None,
            "model": deepcopy((run.extraction_profile or {}).get("agent_config") or (message.checkpoint or {}).get("model") or {"status": "unknown"}) if run else deepcopy((message.checkpoint or {}).get("model") or {"status": "unknown"}),
            "image_hash": payload.image_hash, "source_locator": locator,
            "automatic_original": original, "reason": payload.reason,
            "source": "user_feedback", "counts_as_automatic": False,
        }
        fingerprint = digest([str(message_id), payload.model_dump(mode="json")])
        snapshot["request_fingerprint"] = fingerprint
        await db.execute(insert(AssistantFeedbackSample).values(
            feedback_id=uuid4(), document_id=session.document_id, request_id=payload.request_id,
            qa_message_id=message.message_id, parse_run_id=run.parse_run_id if run else None,
            content_revision_id=message.revision_id, generation_id=message.generation_id,
            sample_type="extraction" if run else "qa", adjudication_status="pending",
            submitted_by=payload.submitted_by, snapshot=snapshot, expected_correction=deepcopy(payload.expected_correction))
            .on_conflict_do_nothing(constraint="uq_assistant_feedback_document_request"))
        sample = await db.scalar(select(AssistantFeedbackSample).where(
            AssistantFeedbackSample.document_id == session.document_id, AssistantFeedbackSample.request_id == payload.request_id))
        if sample.snapshot.get("request_fingerprint") != fingerprint:
            raise ValueError("request_id已提交另一反馈，不能覆盖待裁决样本")
        return {"feedback_id": str(sample.feedback_id), "document_id": str(sample.document_id),
                "request_id": sample.request_id, "adjudication_status": "pending", "source": "user_feedback",
                "counts_as_automatic": False, "rules_changed": False, "facts_changed": False}


feedback_service = FeedbackService()
