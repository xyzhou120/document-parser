"""基于一个冻结索引清单进行混合检索。

单个附件的 JSON 向量使用精确余弦扫描。这里不包含 ANN 或 pgvector 行为；
未来的向量索引可以实现 vector_search。
"""

from __future__ import annotations

import inspect
import math
import re
import unicodedata
from copy import deepcopy
from dataclasses import dataclass, field
from uuid import UUID

from app.services.assistant.index_repository import digest


class IndexUnavailable(ValueError):
    pass


@dataclass
class RetrievalResult:
    generation_id: str
    revision_id: str
    snapshot_hash: str
    manifest: dict
    hits: list[dict]
    service_states: dict
    coverage_status: str
    searched_chunk_count: int
    serve_version: int = 0
    invalidation_watermark: int = 0
    search_finished: bool = False
    warnings: list[str] = field(default_factory=list)


def terms(text):
    normalized = unicodedata.normalize("NFKC", text).lower()
    result = set(re.findall(r"[a-z0-9]+(?:[./-][a-z0-9]+)*|[\u4e00-\u9fff]+", normalized))
    for chinese in re.findall(r"[\u4e00-\u9fff]+", normalized):
        result.update(chinese[index:index + 2] for index in range(len(chinese) - 1))
    return {item for item in result if item.strip()}


def cosine(left, right):
    if len(left) != len(right) or not left or not all(math.isfinite(x) for x in [*left, *right]):
        raise ValueError("embedding vector dimension or values invalid")
    norm = math.sqrt(sum(x * x for x in left) * sum(x * x for x in right))
    return sum(x * y for x, y in zip(left, right, strict=True)) / norm if norm else 0.0


class RetrievalService:
    def __init__(self, repository, *, embedding_provider=None, reranker=None,
                 vector_search=None):
        self.repository, self.embedding_provider = repository, embedding_provider
        self.reranker, self.vector_search = reranker, vector_search or self._exact_vectors

    async def qualify_answer(self, generation_id: UUID | str, *, revision_id,
                             document_id, invalidation_watermark: int,
                             expected_serve_version: int | None = None, db=None) -> dict:
        """除非提供负责答案事务的 db，否则只返回只读提示。

        P16 传入 db 和 expected_serve_version，在同一事务中检查可用性并写入答案。
        锁随后关闭撤销竞争窗口。
        """
        generation = await self.repository.get_generation(generation_id)
        if generation is None or generation.serve_status != "eligible":
            return {"eligible": False, "reason": "generation_unavailable"}
        if db is not None and expected_serve_version is None:
            raise ValueError("answer commit requires expected serve version")
        if db is not None:
            checked = await self.repository.lock_answer_qualification(
                generation.generation_id, expected_revision_id=revision_id,
                expected_serve_version=expected_serve_version,
                expected_invalidation_watermark=invalidation_watermark, document_id=document_id, db=db)
            return dict(checked)
        current = await self.repository.is_generation_current(
            generation.generation_id, revision_id=revision_id,
            document_id=document_id, invalidation_watermark=invalidation_watermark)
        return {"eligible": bool(current), "reason": None if current else "generation_changed",
                "generation_id": str(generation.generation_id),
                "revision_id": str(generation.revision_id),
                "serve_version": generation.serve_version,
                "invalidation_watermark": generation.invalidation_watermark}

    async def search(self, generation_id: UUID | str, query: str, *, limit: int = 12,
                     summary_cue: str | None = None) -> RetrievalResult:
        if not query.strip() or limit <= 0:
            raise ValueError("query and positive result limit required")
        generation = await self.repository.get_generation(generation_id)
        if generation is None or generation.build_status != "succeeded" or generation.serve_status != "eligible":
            raise IndexUnavailable("generation unavailable, staging, or revoked")
        if generation.coverage_status not in {"partial", "full"}:
            raise IndexUnavailable("generation coverage is unknown")
        watermark = generation.invalidation_watermark
        if not await self.repository.is_generation_current(
                generation.generation_id, revision_id=generation.revision_id,
                document_id=generation.document_id, invalidation_watermark=watermark):
            raise IndexUnavailable("generation is not current or source invalidated")
        chunks = generation.chunks
        manifest_ids = {item["chunk_id"] for item in generation.indexed_block_manifest.get("chunks", [])}
        if manifest_ids != {chunk["chunk_id"] for chunk in chunks}:
            raise IndexUnavailable("generation chunks do not match frozen manifest")
        manifest_hashes = {item["chunk_id"]: item["digest"] for item in generation.indexed_block_manifest["chunks"]}
        if any(digest({**chunk, "embedding": None, "embedding_config": None}) != manifest_hashes[chunk["chunk_id"]]
               for chunk in chunks):
            raise IndexUnavailable("generation raw chunks changed")
        query_terms, cue_terms = terms(query), terms(summary_cue or "")
        channel_scores: dict[str, dict[str, float]] = {chunk["chunk_id"]: {} for chunk in chunks}
        for chunk in chunks:
            text_terms = terms(chunk["text"])
            overlap = query_terms & text_terms
            if overlap:
                channel_scores[chunk["chunk_id"]]["keyword"] = len(overlap) / max(1, len(query_terms))
                if chunk.get("table_ref"):
                    channel_scores[chunk["chunk_id"]]["table"] = len(overlap) / max(1, len(query_terms))
            if overlap and cue_terms & text_terms:
                # 摘要可以增加弱提示，但不能排除任何原始块。
                channel_scores[chunk["chunk_id"]]["summary_cue"] = 0.01
        embedding_manifest = generation.indexed_block_manifest.get("embedding", {})
        semantic_status = "unavailable"
        warnings = []
        if self.embedding_provider is not None and embedding_manifest.get("status") == "ready":
            try:
                query_vector = self.embedding_provider.embed_query(query, deepcopy(embedding_manifest))
                if inspect.isawaitable(query_vector):
                    query_vector = await query_vector
                expected_model = embedding_manifest.get("model")
                expected_version = embedding_manifest.get("version")
                for chunk in chunks:
                    cfg = chunk.get("embedding_config") or {}
                    if (expected_model is not None and cfg.get("model") != expected_model) or \
                            (expected_version is not None and cfg.get("version") != expected_version):
                        raise ValueError("embedding configuration mismatch")
                scores = self.vector_search(query_vector, chunks)
                if inspect.isawaitable(scores):
                    scores = await scores
                for chunk_id, score in scores.items():
                    if chunk_id in channel_scores and score > 0:
                        channel_scores[chunk_id]["semantic"] = float(score)
                semantic_status = "ready"
            except Exception as exc:
                semantic_status = "failed"
                warnings.append(f"semantic_search_failed:{type(exc).__name__}")
        ranked = [{**deepcopy(chunk), "channels": channel_scores[chunk["chunk_id"]],
                   "score": sum(channel_scores[chunk["chunk_id"]].values())}
                  for chunk in chunks if channel_scores[chunk["chunk_id"]]]
        ranked.sort(key=lambda item: (-item["score"], item["chunk_id"]))
        rerank_status = "unavailable"
        if self.reranker is not None and ranked:
            try:
                expected_reranker = generation.indexed_block_manifest.get("reranker") or {}
                provider_config = getattr(self.reranker, "config", None)
                if provider_config is not None:
                    if (not expected_reranker.get("model") or
                            provider_config.model != expected_reranker["model"] or
                            provider_config.version != expected_reranker.get("version") or
                            provider_config.endpoint_hash != expected_reranker.get("endpoint_hash")):
                        raise ValueError("reranker snapshot configuration mismatch")
                reranked = self.reranker.rerank(query, deepcopy(ranked))
                if inspect.isawaitable(reranked):
                    reranked = await reranked
                scores = {str(item["chunk_id"]): float(item["score"]) for item in reranked}
                if (not all(math.isfinite(score) for score in scores.values()) or
                        len(scores) != len(reranked) or set(scores) != {item["chunk_id"] for item in ranked}):
                    raise ValueError("reranker score or candidate IDs invalid")
                for item in ranked:
                    item["rerank_score"] = scores.get(item["chunk_id"], item["score"])
                ranked.sort(key=lambda item: (-item["rerank_score"], item["chunk_id"]))
                rerank_status = "ready"
            except Exception as exc:
                rerank_status = "failed"
                warnings.append(f"reranker_failed:{type(exc).__name__}")
        hits = ranked[:limit]
        for hit in hits:
            page = (hit.get("block_ref") or {}).get("page_number")
            hit["context_chunk_ids"] = [chunk["chunk_id"] for chunk in chunks
                                         if chunk["chunk_id"] != hit["chunk_id"] and
                                         abs(int((chunk.get("block_ref") or {}).get("page_number") or 0) - int(page or 0)) <= 1]
            hit["revision_id"], hit["generation_id"] = str(generation.revision_id), str(generation.generation_id)
            hit["citations"] = [{"block_ref": deepcopy(hit.get("block_ref")),
                                  "table_ref": deepcopy(hit.get("table_ref")),
                                  "chunk_id": hit["chunk_id"]}]
            hit.pop("embedding", None)
        # 远程嵌入和重排调用后重新检查；P16 还会在答案提交时检查。
        if not await self.repository.is_generation_current(
                generation.generation_id, revision_id=generation.revision_id,
                document_id=generation.document_id, invalidation_watermark=watermark):
            raise IndexUnavailable("generation revoked during retrieval")
        return RetrievalResult(str(generation.generation_id), str(generation.revision_id),
                               generation.snapshot_hash, deepcopy(generation.indexed_block_manifest),
                               hits, {"keyword": "ready", "table": "ready", "semantic": semantic_status,
                                      "reranker": rerank_status}, generation.coverage_status,
                               len(chunks), serve_version=generation.serve_version,
                               invalidation_watermark=watermark, search_finished=False, warnings=warnings)

    @staticmethod
    def _exact_vectors(query_vector, chunks):
        vector = [float(value) for value in query_vector]
        return {chunk["chunk_id"]: cosine(vector, [float(value) for value in chunk["embedding"]])
                for chunk in chunks if chunk.get("embedding") is not None}
