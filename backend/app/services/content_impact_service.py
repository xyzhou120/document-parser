"""已发布内容修订的只追加影响和失效记录。"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.content import ContentImpact, ContentInvalidation, DocumentProcessingHead


class ContentImpactService:
    async def append_invalidation(
        self, db: AsyncSession, *, document_id: UUID, kind: str, selector: dict[str, Any], reason: str,
    ) -> ContentInvalidation:
        """追加一条失效记录，并在锁定文档头后推进水位。"""
        head = await db.scalar(select(DocumentProcessingHead).where(
            DocumentProcessingHead.document_id == document_id).with_for_update().execution_options(populate_existing=True))
        if head is None:
            head = DocumentProcessingHead(document_id=document_id, cas_version=0, invalidation_watermark=0)
            db.add(head)
            await db.flush()
        # 提供 selector.request_id 时，由它承载请求和重试的幂等键。
        request_id = selector.get("request_id")
        if request_id is not None:
            existing = await db.scalar(select(ContentInvalidation).where(
                ContentInvalidation.document_id == document_id,
                ContentInvalidation.scope_selector["request_id"].as_string() == str(request_id)).limit(1))
            if existing is not None:
                return existing
        row = ContentInvalidation(document_id=document_id, invalidation_kind=kind,
                                  scope_selector=deepcopy(selector), reason=reason,
                                  effective_at=datetime.utcnow())
        db.add(row)
        head.invalidation_watermark = int(head.invalidation_watermark or 0) + 1
        await db.flush()
        return row

    async def append_impact(
        self, db: AsyncSession, *, from_revision_id: UUID, to_revision_id: UUID,
        changed_ranges: list[dict[str, Any]] | None = None, dependency_precision: str = "unknown",
        recompute_policy: str = "full", invalidation_scope: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> ContentImpact:
        if request_id:
            existing = await db.scalar(select(ContentImpact).where(ContentImpact.request_id == request_id).limit(1))
            if existing is not None:
                return existing
        row = ContentImpact(impact_id=uuid4(), from_revision_id=from_revision_id,
                            to_revision_id=to_revision_id, changed_ranges=deepcopy(changed_ranges or []),
                            dependency_precision=dependency_precision, recompute_policy=recompute_policy,
                            invalidation_scope=deepcopy(invalidation_scope or {}), request_id=request_id)
        db.add(row)
        await db.flush()
        return row


content_impact_service = ContentImpactService()
