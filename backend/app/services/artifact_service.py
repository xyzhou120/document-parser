"""可复用的预处理产物，独立于提取规则。"""

import hashlib
import json
from copy import deepcopy
from uuid import UUID

import pymupdf
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.database import DocumentArtifact
from app.parsers.pdf_parser import DEFAULT_MAX_TABLE_CELLS, DEFAULT_RENDER_DPI

# 原生提取、表格几何、OCR 模型或服务、渲染方式变化时都要递增此版本。
# 远程部署的 OCR 模型变更后，操作员也必须递增此值。
PREPROCESS_PROFILE_VERSION = "pdf-blocks-tables-ocr-geometry-v2"


def preprocessing_profile(extraction_engine: str = "rules") -> dict:
    """缓存标识不包含凭据或字段规则。"""
    ocr = settings.models.get("ocr")
    manifest = {
        "algorithm_version": PREPROCESS_PROFILE_VERSION,
        "pymupdf_version": pymupdf.VersionBind,
        "ocr_service": ocr.base_url if ocr else None,
        "ocr_model": ocr.model_name if ocr else None,
        "ocr_model_version": ocr.model_version if ocr else "unknown",
        "render_dpi": DEFAULT_RENDER_DPI, "max_table_cells": DEFAULT_MAX_TABLE_CELLS,
    }
    if extraction_engine == "checklist_agent":
        # 方案 C1（2026-10-10）：扫描页通过区域 OCR 读取，因此 OCR 服务、模型和端点
        # 都属于清单产物标识。只有清单产物改变版本；规则引擎产物保持原版本。
        # 按需生成的来源图片继续使用自己的渲染配置和对象键。
        manifest = {
            "algorithm_version": PREPROCESS_PROFILE_VERSION,
            "pymupdf_version": pymupdf.VersionBind,
            "max_table_cells": DEFAULT_MAX_TABLE_CELLS,
            "ocr_policy": "region-ocr-index-visual-on-demand-v2",
            "ocr_endpoint": "/vl_ocr",
            "ocr_service": ocr.base_url if ocr else None,
            "ocr_model": ocr.model_name if ocr else None,
            "ocr_model_version": ocr.model_version if ocr else "unknown",
        }
    digest = hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"version": "pp-" + digest[:40], "manifest": manifest}


class ArtifactService:
    def adapt_content(self, artifact: DocumentArtifact):
        """为新的消费者返回共享的不可变内容视图。

        此兼容入口保持旧版产物查询、保存及其默认预处理配置不变。
        适配器延迟导入，使现有调用方无需加载或依赖内容修订服务。
        """
        from app.services.content_adapter import content_adapter

        return content_adapter.adapt(artifact)

    async def find_ready(
        self, db: AsyncSession, document_id: UUID, file_hash: str, profile_version: str
    ) -> DocumentArtifact | None:
        result = await db.execute(
            select(DocumentArtifact)
            .where(
                DocumentArtifact.document_id == document_id,
                DocumentArtifact.file_hash == file_hash,
                DocumentArtifact.preprocess_profile_version == profile_version,
                DocumentArtifact.status == "READY",
            )
            .order_by(DocumentArtifact.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def save(
        self,
        db: AsyncSession,
        document_id: UUID,
        file_hash: str,
        profile_version: str,
        pages: list[dict],
        quality: dict,
        manifest: dict | None = None,
    ) -> DocumentArtifact:
        artifact = DocumentArtifact(
            document_id=document_id,
            file_hash=file_hash,
            preprocess_profile_version=profile_version,
            status="PARTIAL" if quality.get("failed_pages") else "READY",
            pages=deepcopy(pages),
            quality={**deepcopy(quality), "preprocess_manifest": manifest or preprocessing_profile()["manifest"],
                     "processing_status": "PARTIAL" if quality.get("failed_pages") else "READY",
                     "source_review_status": "UNVERIFIED"},
        )
        db.add(artifact)
        await db.flush()
        # 提取前先持久化，使重试能够复用成功的预处理结果。
        await db.commit()
        return artifact


artifact_service = ArtifactService()
