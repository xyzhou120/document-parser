import json

import httpx
import pytest

from app.services.assistant.index_repository import MemoryIndexRepository
from app.services.assistant.index_service import IndexService
from app.services.assistant.model_providers import (
    EmbeddingProvider,
    HttpReranker,
    ProviderConfig,
    ProviderUnavailable,
    providers_from_settings,
)
from app.services.assistant.retrieval_service import RetrievalService
from tests.test_assistant_index import FakeContent


def settings(*, embedding=True, reranker=True, version="v1"):
    models = {}
    for kind, enabled, model in (("embedding", embedding, "embed-model"),
                                  ("reranker", reranker, "rank-model")):
        if enabled:
            models[kind] = {"base_url": "https://models.invalid/v1", "model_name": model,
                            "model_version": version, "timeout": 2, "api_key": "test-key",
                            "dimensions": 2 if kind == "embedding" else None}
    return {"models": models}


async def build(providers):
    content = FakeContent()
    repo = MemoryIndexRepository()
    repo.ensure_head(content.revision.document_id, content.revision.revision_id)
    service = IndexService(repository=repo, content_service=content,
                           embedding_provider=providers.embedding)
    result = await service.build(content.revision.revision_id, providers.index_config())
    await service.publish(result.generation.generation_id, expected_cas_version=0,
                          expected_revision_id=content.revision.revision_id,
                          expected_invalidation_watermark=0)
    return result, repo


@pytest.mark.asyncio
async def test_concrete_http_providers_build_vectors_and_hybrid_retrieve_one_manifest():
    requests = []
    def handle(request):
        data = json.loads(request.content)
        requests.append((request.url.path, data, request.headers.get("authorization")))
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(200, json={"model": "embed-model", "data": [{"index": 0, "embedding": [1, 0]}]})
        return httpx.Response(200, json={"model": "rank-model", "results": [
            {"index": index, "relevance_score": float(index + 1)} for index in range(len(data["documents"]))]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        providers = providers_from_settings(settings(), client=client)
        result, repo = await build(providers)
        assert result.embedding_status == "ready"
        assert all(chunk["embedding"] == [1, 0] for chunk in result.generation.chunks)
        retrieved = await RetrievalService(repo, embedding_provider=providers.embedding,
                                           reranker=providers.reranker).search(result.generation.generation_id, "压力")
        assert retrieved.service_states["semantic"] == retrieved.service_states["reranker"] == "ready"
        assert all(request[2] == "Bearer test-key" for request in requests)
        assert len([path for path, _, _ in requests if path.endswith("/embeddings")]) == len(result.generation.chunks) + 1
        assert retrieved.snapshot_hash == result.generation.snapshot_hash
        assert any("semantic" in hit["channels"] for hit in retrieved.hits)


def test_provider_model_version_endpoint_and_route_enter_index_identity():
    first = providers_from_settings(settings())
    changed = providers_from_settings(settings(version="v2"))
    assert first.index_config().config_hash != changed.index_config().config_hash
    assert first.embedding.config.url == "https://models.invalid/v1/embeddings"
    configured = settings()
    configured["models"]["embedding"]["route"] = "/custom/embeddings"
    assert providers_from_settings(configured).index_config().config_hash != first.index_config().config_hash
    assert "test-key" not in repr(first.embedding.config)


@pytest.mark.asyncio
async def test_missing_configuration_keeps_raw_keyword_search_and_explicit_unavailable():
    providers = providers_from_settings(settings(embedding=False, reranker=False))
    result, repo = await build(providers)
    assert providers.embedding is providers.reranker is None
    assert providers.service_states["embedding"]["status"] == "unavailable"
    retrieved = await RetrievalService(repo).search(result.generation.generation_id, "施工钻孔")
    assert retrieved.hits
    assert retrieved.service_states["semantic"] == retrieved.service_states["reranker"] == "unavailable"


@pytest.mark.parametrize("raw", [
    {"index": 1, "embedding": [1, 0]},
    {"index": True, "embedding": [1, 0]},
    {"index": 0, "embedding": []},
    {"index": 0, "embedding": [True, 0]},
    {"index": 0, "embedding": ["1", 0]},
    {"index": 0, "embedding": [0, 0]},
    {"index": 0, "embedding": [1, 2, 3]},
])
@pytest.mark.asyncio
async def test_embedding_rejects_invalid_indices_vectors_and_dimensions(raw):
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"data": [raw]}))) as client:
        providers = providers_from_settings(settings(reranker=False), client=client)
        provider = providers.embedding
        with pytest.raises(ProviderUnavailable):
            await provider.embed("原文", providers.index_config())


@pytest.mark.parametrize("results", [
    [{"index": 0, "relevance_score": .1}],
    [{"index": 0, "relevance_score": .1}, {"index": 0, "relevance_score": .2}],
    [{"index": -1, "relevance_score": .1}, {"index": 1, "relevance_score": .2}],
    [{"index": True, "relevance_score": .1}, {"index": 1, "relevance_score": .2}],
    [{"index": 0, "relevance_score": "1"}, {"index": 1, "relevance_score": .2}],
])
@pytest.mark.asyncio
async def test_reranker_rejects_incomplete_outside_duplicate_and_nonnumeric_results(results):
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"results": results}))) as client:
        provider = providers_from_settings(settings(embedding=False), client=client).reranker
        with pytest.raises(ProviderUnavailable):
            await provider.rerank("问题", [{"chunk_id": "a", "text": "a"}, {"chunk_id": "b", "text": "b"}])


@pytest.mark.asyncio
async def test_embedding_http_failure_never_marks_semantic_ready_or_keeps_partial_vectors():
    calls = 0
    def handle(request):
        nonlocal calls
        calls += 1
        return (httpx.Response(200, json={"data": [{"index": 0, "embedding": [1, 0]}]})
                if calls == 1 else httpx.Response(503, json={"error": "down"}))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        providers = providers_from_settings(settings(reranker=False), client=client)
        result, repo = await build(providers)
        assert result.embedding_status == "failed"
        assert result.generation.indexed_block_manifest["embedding"]["status"] == "failed"
        assert all(chunk["embedding"] is None for chunk in result.generation.chunks)
        retrieved = await RetrievalService(repo, embedding_provider=providers.embedding).search(
            result.generation.generation_id, "压力")
        assert retrieved.hits and retrieved.service_states["semantic"] == "unavailable"
        assert not any("semantic" in hit["channels"] for hit in retrieved.hits)


@pytest.mark.asyncio
async def test_provider_timeout_reports_reason_without_fabricating_vector():
    def timeout(request):
        raise httpx.ReadTimeout("secret response text", request=request)
    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
        providers = providers_from_settings(settings(reranker=False), client=client)
        provider = providers.embedding
        with pytest.raises(ProviderUnavailable, match="embedding_unavailable:timeout") as error:
            await provider.embed("原文", providers.index_config())
        assert "secret" not in str(error.value)


@pytest.mark.asyncio
async def test_embedding_dimension_is_stable_when_no_configured_dimension():
    config = ProviderConfig("https://models.invalid", "embed", "v1", "/embeddings")
    calls = 0
    def handle(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1] * calls}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = EmbeddingProvider(config, client=client)
        manifest = {"model": "embed", "version": "v1", "endpoint_hash": config.endpoint_hash}
        await provider.embed_query("first", manifest)
        with pytest.raises(ProviderUnavailable, match="dimension_mismatch"):
            await provider.embed_query("second", manifest)


@pytest.mark.asyncio
async def test_actual_embedding_provider_rejects_unpinned_endpoint_before_transport():
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1, 0]}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        providers = providers_from_settings(settings(reranker=False), client=client)
        with pytest.raises(ProviderUnavailable, match="snapshot_configuration_mismatch"):
            await providers.embedding.embed_query("原文", {"model": "embed-model", "version": "v1"})
        assert requests == []


def test_invalid_unknown_version_configuration_is_explicitly_unavailable():
    providers = providers_from_settings(settings(version="unknown"))
    assert providers.embedding is providers.reranker is None
    assert providers.service_states["embedding"]["reason"] == "invalid_configuration"


@pytest.mark.asyncio
async def test_reranker_supports_generic_data_score_shape():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200,
            json={"data": [{"index": 1, "score": 2.5}, {"index": 0, "score": .2}]}))) as client:
        provider = HttpReranker(ProviderConfig("https://models.invalid", "rank", "v1", "/rerank"), client=client)
        assert await provider.rerank("q", [{"chunk_id": "a", "text": "a"}, {"chunk_id": "b", "text": "b"}]) == [
            {"chunk_id": "b", "score": 2.5}, {"chunk_id": "a", "score": .2}]


@pytest.mark.asyncio
async def test_reranker_provider_cannot_apply_to_generation_without_pinned_reranker_snapshot():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200,
            json={"results": [{"index": 0, "relevance_score": 1.0}]}))) as client:
        providers = providers_from_settings(settings(embedding=False), client=client)
        result, repo = await build(providers_from_settings(settings(embedding=False, reranker=False)))
        retrieved = await RetrievalService(repo, reranker=providers.reranker).search(
            result.generation.generation_id, "压力")
        assert retrieved.service_states["reranker"] == "failed"
        assert "reranker_failed:ValueError" in retrieved.warnings


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
@pytest.mark.asyncio
async def test_nonfinite_json_vectors_and_scores_never_become_model_output(value):
    def handle(request):
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(200, content='{"data":[{"index":0,"embedding":[' + value + ',1]}]}')
        return httpx.Response(200, content='{"results":[{"index":0,"score":' + value + '}]}')
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        providers = providers_from_settings(settings(), client=client)
        with pytest.raises(ProviderUnavailable, match="invalid_vector"):
            await providers.embedding.embed("text", providers.index_config())
        with pytest.raises(ProviderUnavailable, match="invalid_rerank_score"):
            await providers.reranker.rerank("text", [{"chunk_id": "a", "text": "a"}])
