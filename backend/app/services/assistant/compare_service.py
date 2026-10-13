"""对来源答案与选定事实视图进行显式只读比较。"""

from copy import deepcopy
from uuid import UUID

from sqlalchemy import select

from app.models.assistant import QAMessage, QASession
from app.models.content import ContentInvalidation
from app.models.database import ExtractedFieldModel, ParseRun
from app.models.reviews import FieldReviewRevision
from app.services.fact_service import build_fact_view


class CompareService:
    async def compare(self, db, message_id, *, parse_run_id, view="automatic"):
        if view not in {"automatic", "human"}:
            raise ValueError("比较视图无效")
        message = await db.get(QAMessage, UUID(str(message_id)))
        if message is None:
            raise KeyError("助手消息不存在")
        session = await db.get(QASession, message.session_id)
        run = await db.get(ParseRun, UUID(str(parse_run_id)))
        if session is None or run is None or run.document_id != session.document_id:
            raise ValueError("比较双方必须属于同一附件")
        fields = list((await db.execute(select(ExtractedFieldModel).where(ExtractedFieldModel.parse_run_id == run.parse_run_id))).scalars().all())
        reviews = list((await db.execute(select(FieldReviewRevision).where(FieldReviewRevision.parse_run_id == run.parse_run_id))).scalars().all())
        invalidations = list((await db.execute(select(ContentInvalidation).where(ContentInvalidation.document_id == run.document_id))).scalars().all())
        facts = build_fact_view(run, fields, view="reviewed" if view == "human" else "automatic", revisions=reviews, invalidations=invalidations)
        mismatch = run.content_revision_id != message.revision_id
        warnings = ["原文答案和提取事实来自不同内容版本，请分别核对"] if mismatch else []
        if message.execution_state != "succeeded":
            warnings.append("助手答案尚未完整完成")
        return {
            "document_id": str(run.document_id), "qa_message_id": str(message.message_id), "parse_run_id": str(run.parse_run_id),
            "view": view, "comparison_kind": "explicit_side_by_side", "writes_facts": False,
            "version_mismatch": mismatch, "warnings": warnings,
            "source_answer": {"answer": message.answer, "claims": deepcopy(message.claims or []),
                              "citations": deepcopy(message.citations or []), "revision_id": str(message.revision_id),
                              "generation_id": str(message.generation_id) if message.generation_id else None,
                              "execution_status": message.execution_state, "source": "document_original"},
            "fact_view": facts.model_dump(mode="json"),
            "human_source": view == "human", "automatic_acceptance": False,
        }


compare_service = CompareService()
