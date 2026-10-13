"""Reusable preprocessing artifacts, independent of extraction rules."""

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

# Bump whenever native extraction, table geometry, OCR model/service or rendering changes.
# Operators must bump this value after changing a remotely deployed OCR model.
PREPROCESS_PROFILE_VERSION = "pdf-blocks-tables-ocr-geometry-v2"


def preprocessing_profile(extraction_engine: str = "rules") -> dict:
    """No credentials or field rules enter the cache identity."""
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
        # Plan C1 (2026-10-10): scanned pages are read by region OCR, so the OCR service, model
        # and endpoint are part of the checklist artifact identity.  Only checklist artifacts
        # change version; rules-engine artifacts keep theirs.  On-demand source images keep their
        # own render profile/object keys.
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
        """Return the shared immutable content view for new consumers.

        This compatibility entry point leaves legacy artifact lookup/save and its
        default preprocessing profile untouched.  The adapter is imported lazily so
        existing callers do not pay for or depend on content revision services.
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
        # Persist before extraction; a retry can reuse successful preprocessing.
        await db.commit()
        return artifact


artifact_service = ArtifactService()
