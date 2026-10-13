import asyncio
from copy import deepcopy
from dataclasses import dataclass
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services.assistant.index_repository import MemoryIndexRepository
from app.services.assistant.index_service import IndexConfig, IndexService
from app.services.assistant.retrieval_service import IndexUnavailable, RetrievalService
from app.services.content_adapter import ContentAdapter


def artifact(*, complete=True, text="瓦斯压力 1.25 MPa 施工钻孔"):
    doc, aid = uuid4(), uuid4()
    quality = {"page_count": 2 if not complete else 1,
               "coverage_manifest": {"expected_page_count": 1 if complete else 2,
                                      "pages": [{"page_number": 1, "scope_complete": complete,
                                                 "regions": [{"region_id": "all", "state": "processed"}] if complete else []}]}}
    return SimpleNamespace(artifact_id=aid, document_id=doc, file_hash="f" * 64, status="READY",
                           quality=quality, pages=[{"page_number": 1, "text": text,
                           "text_blocks": [{"block_id": "b1", "text": text, "raw_text": text,
                                            "source": "native", "bbox": [1, 2, 30, 40]}],
                           "tables": [{"table_id": "t1", "title": "压力", "header": {"单位": "MPa"},
                                       "rows": [["施工钻孔", "1.25 MPa"]], "cells": []}]}])


@dataclass
class Revision:
    revision_id: object
    document_id: object
    content_digest: str
    coverage_digest: str
    adapter_version: str


class FakeContent:
    def __init__(self, complete=True):
        source = artifact(complete=complete)
        content = ContentAdapter().adapt(source)
        self.revision = Revision(uuid4(), source.document_id, content.content_digest,
                                 content.coverage_digest, content.adapter_version)
        self.content = content

    async def read_revision(self, *args):
        return SimpleNamespace(revision=self.revision, content=self.content)


class Embedder:
    def embed(self, text, config):
        return [1.0, 0.0] if "压力" in text else [0.0, 1.0]

    def embed_query(self, query, config):
        return [1.0, 0.0]


def setup(complete=True, provider=None):
    content = FakeContent(complete)
    repository = MemoryIndexRepository()
    repository.ensure_head(content.revision.document_id, content.revision.revision_id)
    service = IndexService(repository=repository, content_service=content, embedding_provider=provider)
    return service, repository, content


def test_make_chunks_keeps_raw_block_ids_table_relationship_and_paginates_long_text():
    chunks = IndexService.make_chunks([{"page_number": 1, "text_blocks": [{"block_id": "b", "text": "x" * 150}]}],
                                      IndexConfig(chunk_chars=64))
    assert [chunk["chunk_id"] for chunk in chunks] == ["block:b:offset:0", "block:b:offset:128", "block:b:offset:64"]
    assert all(chunk["block_ref"]["block_id"] == "b" for chunk in chunks)


def test_same_revision_config_and_snapshot_is_idempotent_and_partial_publication_freezes_scope():
    async def run():
        service, repository, content = setup(complete=False)
        first = await service.build(content.revision.revision_id, IndexConfig())
        second = await service.build(content.revision.revision_id, IndexConfig())
        assert first.generation.generation_id == second.generation.generation_id
        assert first.generation.coverage_status == "partial"
        published = await service.publish(first.generation.generation_id, expected_cas_version=0,
                                          expected_revision_id=content.revision.revision_id,
                                          expected_invalidation_watermark=0)
        assert published.serve_status == "eligible" and published.coverage_status == "partial"
        assert first.generation.serve_status == "unavailable"
    asyncio.run(run())


def test_config_or_more_blocks_creates_a_new_generation_and_old_is_frozen():
    async def run():
        service, repository, content = setup()
        first = await service.build(content.revision.revision_id, IndexConfig(chunk_chars=100))
        second = await service.build(content.revision.revision_id, IndexConfig(chunk_chars=64))
        assert first.generation.generation_id != second.generation.generation_id
        assert first.generation.serve_status == "unavailable"
        assert second.generation.serve_status == "unavailable"
    asyncio.run(run())


def test_publish_requires_active_revision_and_cas_then_retrieval_uses_same_generation():
    async def run():
        service, repository, content = setup(provider=Embedder())
        built = await service.build(content.revision.revision_id, IndexConfig(embedding_model="e", embedding_version="1"))
        await service.publish(built.generation.generation_id, expected_cas_version=0,
                              expected_revision_id=content.revision.revision_id,
                              expected_invalidation_watermark=0)
        result = await RetrievalService(repository, embedding_provider=Embedder()).search(
            built.generation.generation_id, "瓦斯压力", limit=5)
        assert result.snapshot_hash == built.generation.snapshot_hash
        assert result.service_states["semantic"] == "ready"
        assert result.hits and result.hits[0]["revision_id"] == str(content.revision.revision_id)
        with pytest.raises(ValueError, match="guard"):
            await service.publish(built.generation.generation_id, expected_cas_version=0,
                                  expected_revision_id=content.revision.revision_id,
                                  expected_invalidation_watermark=0)
    asyncio.run(run())


def test_staging_generation_cannot_query_and_missing_embedding_is_explicit():
    async def run():
        service, repository, content = setup()
        built = await service.build(content.revision.revision_id, IndexConfig())
        with pytest.raises(IndexUnavailable):
            await RetrievalService(repository).search(built.generation.generation_id, "压力")
        assert built.embedding_status == "unavailable"
    asyncio.run(run())


def test_invalidation_and_active_pointer_prevent_old_generation_late_query():
    async def run():
        service, repository, content = setup()
        built = await service.build(content.revision.revision_id)
        await service.publish(built.generation.generation_id, expected_cas_version=0,
                              expected_revision_id=content.revision.revision_id,
                              expected_invalidation_watermark=0)
        await repository.invalidate_generation(built.generation.generation_id)
        with pytest.raises(IndexUnavailable):
            await RetrievalService(repository).search(built.generation.generation_id, "压力")
    asyncio.run(run())


def test_invalidation_selector_blocks_matching_page_but_keeps_unrelated_generation_eligible():
    async def run():
        service, repository, content = setup()
        built = await service.build(content.revision.revision_id)
        await service.publish(built.generation.generation_id, expected_cas_version=0,
                              expected_revision_id=content.revision.revision_id,
                              expected_invalidation_watermark=0)
        # The manifest has only page 1; page 9 is outside this generation scope.
        from app.services.assistant.index_repository import invalidation_applies
        invalidation = SimpleNamespace(scope_selector={"pages": [9]}, invalidation_kind="ocr", effective_at=None)
        assert not invalidation_applies(invalidation, built.generation)
        invalidation.scope_selector = {"pages": [1]}
        assert invalidation_applies(invalidation, built.generation)
    asyncio.run(run())


def test_answer_qualification_rechecks_active_pointer_and_watermark():
    async def run():
        service, repository, content = setup()
        built = await service.build(content.revision.revision_id)
        await service.publish(built.generation.generation_id, expected_cas_version=0,
                              expected_revision_id=content.revision.revision_id,
                              expected_invalidation_watermark=0)
        retrieval = RetrievalService(repository)
        assert (await retrieval.qualify_answer(built.generation.generation_id,
                                                revision_id=content.revision.revision_id,
                                                document_id=content.revision.document_id,
                                                invalidation_watermark=0))["eligible"]
        repository.heads[content.revision.document_id].invalidation_watermark = 1
        result = await retrieval.qualify_answer(built.generation.generation_id,
                                                revision_id=content.revision.revision_id,
                                                document_id=content.revision.document_id,
                                                invalidation_watermark=0)
        assert not result["eligible"] and result["reason"] == "generation_changed"
    asyncio.run(run())


def test_raw_block_and_table_still_recall_when_semantic_service_unavailable():
    async def run():
        service, repository, content = setup()
        built = await service.build(content.revision.revision_id)
        await service.publish(built.generation.generation_id, expected_cas_version=0,
                              expected_revision_id=content.revision.revision_id,
                              expected_invalidation_watermark=0)
        result = await RetrievalService(repository).search(built.generation.generation_id, "施工钻孔")
        assert result.hits
        assert result.service_states["semantic"] == "unavailable"
        assert any(hit["citations"][0]["block_ref"] is not None for hit in result.hits)
    asyncio.run(run())


def test_summary_is_soft_cue_and_does_not_add_nonexistent_raw_chunks():
    async def run():
        service, repository, content = setup()
        built = await service.build(content.revision.revision_id)
        await service.publish(built.generation.generation_id, expected_cas_version=0,
                              expected_revision_id=content.revision.revision_id,
                              expected_invalidation_watermark=0)
        result = await RetrievalService(repository).search(built.generation.generation_id, "不存在", summary_cue="压力")
        assert result.hits == []
    asyncio.run(run())


def test_partial_selected_blocks_are_frozen_and_more_blocks_create_new_generation():
    async def run():
        service, repository, content = setup()
        first = await service.build(content.revision.revision_id, block_ids={"b1"})
        original = deepcopy(first.generation.chunks)
        await service.publish(first.generation.generation_id, expected_cas_version=0,
                              expected_revision_id=content.revision.revision_id,
                              expected_invalidation_watermark=0)
        second = await service.build(content.revision.revision_id)
        assert first.generation.generation_id != second.generation.generation_id
        assert first.generation.coverage_status == "partial" and second.generation.coverage_status == "full"
        assert (await repository.get_generation(first.generation.generation_id)).chunks == original
    asyncio.run(run())


def test_older_build_cannot_publish_after_new_generation_even_with_refreshed_cas():
    async def run():
        service, repository, content = setup()
        older = await service.build(content.revision.revision_id, block_ids={"b1"})
        newer = await service.build(content.revision.revision_id)
        await service.publish(newer.generation.generation_id, expected_cas_version=0,
                              expected_revision_id=content.revision.revision_id,
                              expected_invalidation_watermark=0)
        with pytest.raises(ValueError, match="CAS"):
            await service.publish(older.generation.generation_id, expected_cas_version=1,
                                  expected_revision_id=content.revision.revision_id,
                                  expected_invalidation_watermark=0)
        assert repository.heads[content.revision.document_id].active_index_generation_id == newer.generation.generation_id
    asyncio.run(run())


def test_unknown_coverage_cannot_publish_or_query():
    async def run():
        service, repository, content = setup()
        content.content.coverage_manifest["status"] = "unknown"
        content.content.coverage_manifest["can_claim_absence"] = False
        built = await service.build(content.revision.revision_id)
        assert built.generation.coverage_status == "unknown"
        with pytest.raises(ValueError):
            await service.publish(built.generation.generation_id, expected_cas_version=0,
                                  expected_revision_id=content.revision.revision_id,
                                  expected_invalidation_watermark=0)
        with pytest.raises(IndexUnavailable):
            await RetrievalService(repository).search(built.generation.generation_id, "压力")
    asyncio.run(run())


def test_semantic_remote_revocation_during_search_rejects_late_result():
    async def run():
        service, repository, content = setup(provider=Embedder())
        built = await service.build(content.revision.revision_id, IndexConfig(embedding_model="e", embedding_version="1"))
        await service.publish(built.generation.generation_id, expected_cas_version=0,
                              expected_revision_id=content.revision.revision_id,
                              expected_invalidation_watermark=0)
        class InvalidatingEmbedder:
            async def embed_query(self, text, config):
                await repository.invalidate_generation(built.generation.generation_id)
                return [1, 0]
        with pytest.raises(IndexUnavailable, match="revoked"):
            await RetrievalService(repository, embedding_provider=InvalidatingEmbedder()).search(
                built.generation.generation_id, "压力")
    asyncio.run(run())


def test_semantic_and_reranker_failure_preserve_raw_keyword_hits_with_explicit_status():
    async def run():
        service, repository, content = setup(provider=Embedder())
        built = await service.build(content.revision.revision_id, IndexConfig(embedding_model="e", embedding_version="1"))
        await service.publish(built.generation.generation_id, expected_cas_version=0,
                              expected_revision_id=content.revision.revision_id,
                              expected_invalidation_watermark=0)
        class Broken:
            def embed_query(self, *args):
                raise TimeoutError()
            def rerank(self, *args):
                raise TimeoutError()
        result = await RetrievalService(repository, embedding_provider=Broken(), reranker=Broken()).search(
            built.generation.generation_id, "压力")
        assert result.hits and result.service_states["semantic"] == result.service_states["reranker"] == "failed"
        assert result.search_finished is False
    asyncio.run(run())


def test_raw_text_tail_remains_indexed_and_manifest_tampering_rejected():
    async def run():
        service, repository, content = setup()
        content.content.pages[0]["text_blocks"][0]["raw_text"] = "原文" * 200 + "唯一末尾标记"
        built = await service.build(content.revision.revision_id, IndexConfig(chunk_chars=64))
        await service.publish(built.generation.generation_id, expected_cas_version=0,
                              expected_revision_id=content.revision.revision_id,
                              expected_invalidation_watermark=0)
        result = await RetrievalService(repository).search(built.generation.generation_id, "唯一末尾标记")
        assert any("唯一末尾标记" in hit["text"] for hit in result.hits)
        repository.generations[built.generation.generation_id].chunks[0]["text"] = "修改后的原文"
        with pytest.raises(IndexUnavailable, match="changed"):
            await RetrievalService(repository).search(built.generation.generation_id, "压力")
    asyncio.run(run())


def test_embedding_version_has_new_config_and_inconsistent_vectors_abort_build():
    async def run():
        service, repository, content = setup(provider=Embedder())
        one = await service.build(content.revision.revision_id, IndexConfig(embedding_model="e", embedding_version="1"))
        two = await service.build(content.revision.revision_id, IndexConfig(embedding_model="e", embedding_version="2"))
        assert one.generation.generation_id != two.generation.generation_id
        class Mixed:
            def __init__(self):
                self.calls = 0
            def embed(self, text, config):
                self.calls += 1
                return [1] if self.calls == 1 else [1, 2]
        service.embedding_provider = Mixed()
        failed = await service.build(content.revision.revision_id, IndexConfig(embedding_model="e", embedding_version="3"))
        assert failed.embedding_status == "failed"
        assert all(chunk["embedding"] is None for chunk in failed.generation.chunks)
    asyncio.run(run())
