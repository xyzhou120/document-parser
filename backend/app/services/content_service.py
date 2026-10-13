"""内容修订读取与产物到修订的不可变绑定。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.content import ContentRevision
from app.models.database import DocumentArtifact
from app.models.source_revisions import SourceRevision
from app.services.content_adapter import AdaptedContent, ContentAdapter


@dataclass(frozen=True)
class RevisionContent:
    revision: ContentRevision
    content: AdaptedContent


class ContentRevisionError(ValueError):
    """无法按记录内容读取请求的不可变修订。"""


class ContentService:
    def __init__(self, adapter: ContentAdapter | None = None):
        self.adapter = adapter or ContentAdapter()

    async def get_revision(self, db: AsyncSession, revision_id: UUID | str) -> RevisionContent:
        """只读取指定的 revision_id，不能替换为当前或最新修订。"""
        revision = await db.get(ContentRevision, UUID(str(revision_id)))
        if revision is None:
            raise ContentRevisionError(f"content revision {revision_id} does not exist")
        artifact = await db.get(DocumentArtifact, revision.artifact_id)
        if artifact is None:
            raise ContentRevisionError(f"artifact for content revision {revision_id} does not exist")
        if artifact.document_id != revision.document_id or artifact.file_hash != revision.file_hash:
            raise ContentRevisionError(f"artifact identity no longer matches revision {revision_id}")
        if self.adapter.version != revision.adapter_version:
            raise ContentRevisionError(f"unsupported adapter version {revision.adapter_version}")
        content = self.adapter.adapt(artifact)
        if content.content_digest != revision.content_digest:
            raise ContentRevisionError(f"artifact content no longer matches revision {revision_id}")
        if content.coverage_digest != revision.coverage_digest:
            raise ContentRevisionError(f"artifact coverage no longer matches revision {revision_id}")
        return RevisionContent(revision=revision, content=content)

    # 供索引和助手调用方使用的显式别名，用于明确绑定语义。
    read_revision = get_revision
    get = get_revision

    def adapt_artifact(self, artifact: DocumentArtifact) -> AdaptedContent:
        return self.adapter.adapt(artifact)

    async def get_by_source_revision(self, db: AsyncSession, source_revision_id: UUID | str) -> RevisionContent | None:
        """解析唯一的只读 SourceRevision -> ContentRevision 映射。"""
        result = await db.execute(
            select(ContentRevision).where(
                ContentRevision.origin_source_revision_id == UUID(str(source_revision_id))
            )
        )
        revision = result.scalar_one_or_none()
        if revision is None:
            return None
        return await self.get_revision(db, revision.revision_id)

    async def pin_revision(
        self,
        db: AsyncSession,
        artifact: DocumentArtifact,
        *,
        parent_revision_id: UUID | str | None = None,
        origin_source_revision_id: UUID | str | None = None,
        origin_event_id: str | None = None,
        source_kind: str = "artifact",
        change_kind: str = "initial",
    ) -> RevisionContent:
        """为一个 Artifact 创建或复用不可变修订。

        此函数只记录来源修订关系；不会更新 SourceRevision 记录，也不会改变文档处理头。
        """
        # 串行处理此不可变 Artifact 的绑定尝试，包括唯一的 SourceRevision 映射。
        # 这里只刷新，不提交；事务由发布方负责。
        persisted = await db.get(DocumentArtifact, artifact.artifact_id, with_for_update=True)
        if persisted is None:
            raise ContentRevisionError("only persisted artifacts can be pinned")
        artifact = persisted
        content = self.adapter.adapt(artifact)
        document_id = UUID(str(artifact.document_id))
        parent = UUID(str(parent_revision_id)) if parent_revision_id else None
        origin = UUID(str(origin_source_revision_id)) if origin_source_revision_id else None
        if parent is not None:
            parent_revision = await db.get(ContentRevision, parent)
            if parent_revision is None or parent_revision.document_id != document_id or parent_revision.file_hash != artifact.file_hash:
                raise ContentRevisionError("parent revision does not belong to this document source")
        if origin is not None:
            source = await db.get(SourceRevision, origin)
            if source is None or source.document_id != document_id or source.derived_artifact_id != artifact.artifact_id:
                raise ContentRevisionError("source revision does not map to this derived artifact")
            result = await db.execute(select(ContentRevision).where(ContentRevision.origin_source_revision_id == origin))
            mapped = result.scalar_one_or_none()
            if mapped is not None:
                if mapped.artifact_id != artifact.artifact_id or mapped.parent_revision_id != parent:
                    raise ContentRevisionError("source revision is already pinned to another lineage")
                return await self.get_revision(db, mapped.revision_id)
        conditions = [
            ContentRevision.document_id == document_id,
            ContentRevision.artifact_id == artifact.artifact_id,
            ContentRevision.content_digest == content.content_digest,
            ContentRevision.coverage_digest == content.coverage_digest,
            ContentRevision.adapter_version == content.adapter_version,
            ContentRevision.parent_revision_id == parent,
            ContentRevision.origin_source_revision_id == origin,
        ]
        conditions.append(ContentRevision.origin_event_id == origin_event_id)
        result = await db.execute(select(ContentRevision).where(*conditions).limit(1))
        existing = result.scalar_one_or_none()
        if existing is not None:
            return RevisionContent(existing, content)

        revision = ContentRevision(
            revision_id=uuid4(),
            document_id=document_id,
            artifact_id=artifact.artifact_id,
            parent_revision_id=parent,
            origin_source_revision_id=origin,
            origin_event_id=origin_event_id,
            file_hash=str(artifact.file_hash),
            content_digest=content.content_digest,
            preprocess_manifest=deepcopy((getattr(artifact, "quality", None) or {}).get("preprocess_manifest", {})),
            adapter_version=content.adapter_version,
            source_kind=source_kind,
            change_kind=change_kind,
            coverage_manifest=deepcopy(content.coverage_manifest),
            coverage_digest=content.coverage_digest,
        )
        db.add(revision)
        await db.flush()
        return RevisionContent(revision, content)


content_service = ContentService()
