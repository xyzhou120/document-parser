"""构建不可变且绑定版本的助手索引。"""

from __future__ import annotations

import inspect
import math
from copy import deepcopy
from dataclasses import dataclass, field
from uuid import UUID

from app.services.assistant.index_repository import GenerationSnapshot, IndexRepository, digest
from app.services.content_service import ContentService


@dataclass(frozen=True)
class IndexConfig:
    version: str = "assistant-index-v1"
    chunk_chars: int = 1200
    embedding_model: str | None = None
    embedding_version: str | None = None
    embedding_endpoint_hash: str | None = None
    reranker: str | None = None
    reranker_model: str | None = None
    reranker_version: str | None = None
    reranker_endpoint_hash: str | None = None
    language: str = "zh"
    include_tables: bool = True
    include_ocr_regions: bool = True

    def __post_init__(self):
        if self.chunk_chars < 64:
            raise ValueError("chunk_chars must be at least 64")

    @property
    def config_hash(self) -> str:
        return digest({"version": self.version, **self.__dict__})


@dataclass
class IndexBuildResult:
    generation: GenerationSnapshot
    created: bool
    embedding_status: str
    coverage_status: str
    service_state: dict = field(default_factory=dict)


class IndexService:
    def __init__(self, sessions=None, *, content_service: ContentService | None = None,
                 repository: IndexRepository | None = None, embedding_provider=None):
        if repository is None and sessions is None:
            raise ValueError("sessions or repository required")
        self.content_service = content_service or ContentService()
        self.sessions = sessions
        self.repository = repository or IndexRepository(sessions)
        self.embedding_provider = embedding_provider

    async def build(self, revision_id: UUID | str, config: IndexConfig | None = None,
                    *, block_ids: set[str] | None = None,
                    invalidation_watermark: int | None = None) -> IndexBuildResult:
        config = config or IndexConfig()
        revision_content = await self._read_revision(revision_id)
        revision, content = revision_content.revision, revision_content.content
        head = await self.repository.get_head(revision.document_id)
        if invalidation_watermark is None:
            invalidation_watermark = head.invalidation_watermark if head else 0
        chunks = self.make_chunks(content.pages, config)
        if block_ids is not None:
            known = {chunk["chunk_id"] for chunk in chunks} | {
                str((chunk.get("block_ref") or {}).get("block_id")) for chunk in chunks} | {
                str((chunk.get("table_ref") or {}).get("table_id")) for chunk in chunks}
            if not block_ids <= known:
                raise ValueError("requested block is outside pinned revision")
            chunks = [chunk for chunk in chunks if chunk["chunk_id"] in block_ids or
                      (chunk.get("block_ref") or {}).get("block_id") in block_ids or
                      (chunk.get("table_ref") or {}).get("table_id") in block_ids]
        source_coverage = str(content.coverage_manifest.get("status", "unknown"))
        coverage = ("full" if content.coverage_complete and block_ids is None else
                    "unknown" if source_coverage == "unknown" else "partial")
        if coverage == "full" and ((not config.include_tables and any(page.get("tables") for page in content.pages)) or
                                   (not config.include_ocr_regions and any(page.get("regions") for page in content.pages))):
            coverage = "partial"
        semantic = ("ready" if self.embedding_provider is not None and
                    config.embedding_model and config.embedding_version else "unavailable")
        embedding_error = None
        manifest = {
            "revision_id": str(revision.revision_id),
            "document_id": str(revision.document_id),
            "content_digest": revision.content_digest,
            "coverage_digest": revision.coverage_digest,
            "adapter_version": revision.adapter_version,
            "config_version": config.version,
            "config_hash": config.config_hash,
            "block_ids": sorted({chunk["block_ref"].get("block_id", chunk["chunk_id"]) for chunk in chunks}),
            "block_manifest": [{"block_id": (chunk.get("block_ref") or {}).get("block_id"),
                                "page_number": (chunk.get("block_ref") or {}).get("page_number"),
                                "chunk_id": chunk["chunk_id"]} for chunk in chunks],
            "chunks": [{"chunk_id": chunk["chunk_id"], "digest": digest(chunk)} for chunk in chunks],
            "coverage_status": coverage,
            "source_coverage": deepcopy(content.coverage_manifest),
            "embedding": {"status": semantic, "model": config.embedding_model,
                          "version": config.embedding_version,
                          "endpoint_hash": config.embedding_endpoint_hash,
                          "error": embedding_error},
            "reranker": {"model": config.reranker_model, "version": config.reranker_version,
                         "endpoint_hash": config.reranker_endpoint_hash},
            "table_context": True,
        }
        snapshot_hash = digest(manifest)
        manifest["publication_cas_version"] = head.cas_version if head else None
        existing = await self.repository.find_generation(revision.revision_id, config.config_hash, snapshot_hash)
        if existing:
            return IndexBuildResult(existing, False, semantic, existing.coverage_status,
                                    {"keyword": "ready", "table": "ready", "semantic": semantic,
                                     "reranker": "unavailable"})
        if semantic == "ready":
            try:
                chunks = await self._embed(chunks, config)
            except Exception as exc:
                semantic, embedding_error = "failed", type(exc).__name__
                # 索引版本不能在向量子集不完整时声称语义索引成功。
                for chunk in chunks:
                    chunk["embedding"], chunk["embedding_config"] = None, None
                manifest["embedding"] = {**manifest["embedding"], "status": semantic,
                                         "error": embedding_error}
                snapshot_hash = digest({key: value for key, value in manifest.items()
                                        if key != "publication_cas_version"})
        generation = await self.repository.create_generation(
            revision_id=revision.revision_id, document_id=revision.document_id,
            config_hash=config.config_hash, snapshot_hash=snapshot_hash,
            manifest=manifest, chunks=chunks, coverage_status=manifest["coverage_status"],
            invalidation_watermark=invalidation_watermark)
        return IndexBuildResult(generation, True, semantic, generation.coverage_status,
                                 {"keyword": "ready", "table": "ready",
                                 "semantic": semantic,
                                 "reranker": "unavailable" if not config.reranker else "unconfigured",
                                 **({"semantic_error": embedding_error} if embedding_error else {})})

    async def publish(self, generation_id, *, expected_cas_version: int,
                      expected_revision_id, expected_invalidation_watermark: int):
        return await self.repository.publish_generation(
            generation_id, expected_cas_version=expected_cas_version,
            expected_revision_id=expected_revision_id,
            expected_invalidation_watermark=expected_invalidation_watermark)

    @staticmethod
    def make_chunks(pages: list[dict], config: IndexConfig, *, block_ids: set[str] | None = None):
        chunks: list[dict] = []
        for page in sorted(pages, key=lambda p: int(p.get("page_number", 0))):
            page_number = int(page.get("page_number", 0))
            blocks = list(page.get("text_blocks") or [])
            if not blocks and page.get("text"):
                blocks = [{"block_id": f"page-{page_number}", "text": page.get("text"),
                           "raw_text": page.get("text"), "source": "native"}]
            for block in blocks:
                block_id = str(block.get("block_id") or f"page-{page_number}")
                chunk_id = f"block:{block_id}"
                if block_ids is not None and chunk_id not in block_ids and block_id not in block_ids:
                    continue
                text = str(block.get("raw_text", block.get("text", "")) or "").strip()
                if not text:
                    continue
                if not config.include_ocr_regions and block.get("source") == "ocr":
                    continue
                for offset in range(0, len(text), config.chunk_chars):
                    segment_id = chunk_id if len(text) <= config.chunk_chars else f"{chunk_id}:offset:{offset}"
                    chunks.append({"chunk_id": segment_id, "text": text[offset:offset + config.chunk_chars],
                               "block_ref": {"block_id": block_id, "page_number": page_number,
                                             "source": block.get("source", "native"),
                                             "region_id": block.get("region_id"),
                                             "bbox": deepcopy(block.get("bbox")),
                                             "physical_page": page_number,
                                             "start_offset": offset, "end_offset": min(offset + config.chunk_chars, len(text))},
                               "table_ref": None, "embedding": None,
                               "embedding_config": None})
            if config.include_tables:
                for table in page.get("tables") or []:
                    table_id = str(table.get("table_id") or f"page-{page_number}-table")
                    chunk_id = f"table:{table_id}"
                    if block_ids is not None and chunk_id not in block_ids and table_id not in block_ids:
                        continue
                    rows = table.get("rows") or []
                    header = table.get("header") or {}
                    cells = table.get("cells") or []
                    text = " ".join([str(table.get("title") or ""), str(header),
                                     " ".join(" ".join(str(x) for x in row) if isinstance(row, list) else str(row)
                                              for row in rows),
                                     " ".join(str(cell.get("text", "")) for cell in cells)]).strip()
                    if text:
                        for offset in range(0, len(text), config.chunk_chars):
                            segment_id = chunk_id if len(text) <= config.chunk_chars else f"{chunk_id}:offset:{offset}"
                            chunks.append({"chunk_id": segment_id, "text": text[offset:offset + config.chunk_chars],
                                       "block_ref": {"page_number": page_number, "physical_page": page_number},
                                       "table_ref": {"table_id": table_id, "page_number": page_number,
                                                      "header": deepcopy(header),
                                                      "row_ids": deepcopy(table.get("row_ids") or []),
                                                      "cell_ids": [cell.get("cell_id") for cell in cells],
                                                      "start_offset": offset, "end_offset": min(offset + config.chunk_chars, len(text))},
                                       "embedding": None, "embedding_config": None})
        # 稳定顺序属于快照身份和搜索分页契约的一部分。
        if len({item["chunk_id"] for item in chunks}) != len(chunks):
            raise ValueError("duplicate block/table identity in revision")
        return sorted(chunks, key=lambda item: item["chunk_id"])

    async def _read_revision(self, revision_id):
        if self.sessions is None:
            return await self.content_service.read_revision(None, revision_id)
        async with self.sessions() as db:
            return await self.content_service.read_revision(db, revision_id)

    async def _embed(self, chunks, config):
        if self.embedding_provider is None:
            return chunks
        dimension = None
        for chunk in chunks:
            value = self.embedding_provider.embed(chunk["text"], config)
            if inspect.isawaitable(value):
                value = await value
            if value is None:
                raise ValueError("embedding service returned no vector")
            vector = [float(x) for x in value]
            if not vector or not all(math.isfinite(x) for x in vector):
                raise ValueError("embedding service returned invalid vector")
            if dimension is not None and len(vector) != dimension:
                raise ValueError("embedding dimension changed within generation")
            dimension = len(vector)
            chunk["embedding"], chunk["embedding_config"] = vector, {
                "model": config.embedding_model, "version": config.embedding_version,
                "dimension": len(vector),
            }
        return chunks
