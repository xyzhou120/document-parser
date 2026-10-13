"""HTTP embedding/reranking adapters with explicit versioned configuration.

Clients may be injected for MockTransport tests. Adapters never fabricate vectors
or scores, and errors expose a reason code rather than response bodies or keys.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.services.assistant.index_repository import digest
from app.services.assistant.index_service import IndexConfig


class ProviderUnavailable(RuntimeError):
    def __init__(self, kind: str, reason: str):
        self.kind, self.reason = kind, reason
        super().__init__(f"{kind}_unavailable:{reason}")


@dataclass(frozen=True)
class ProviderConfig:
    base_url: str
    model: str
    version: str
    route: str
    api_key: str | None = field(default=None, repr=False)
    timeout: float = 60
    dimensions: int | None = None

    def __post_init__(self):
        parsed = urlsplit(self.base_url)
        if (parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or
                parsed.password or parsed.query or parsed.fragment):
            raise ValueError("provider base_url must be an HTTP URL without credentials or query")
        if (not isinstance(self.model, str) or not isinstance(self.version, str) or
                not self.model.strip() or self.version.strip().lower() in {"", "unknown"}):
            raise ValueError("provider requires a pinned model and version")
        if not self.route or "://" in self.route or ".." in self.route or "?" in self.route or "#" in self.route:
            raise ValueError("provider route must be a relative endpoint path")
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("provider timeout must be positive and finite")
        if self.dimensions is not None and (not isinstance(self.dimensions, int) or
                                            isinstance(self.dimensions, bool) or self.dimensions <= 0):
            raise ValueError("embedding dimensions must be positive")

    @property
    def url(self) -> str:
        return f"{self.base_url.rstrip('/')}/{self.route.lstrip('/')}"

    @property
    def endpoint_hash(self) -> str:
        return digest({"base_url": self.base_url.rstrip("/"), "route": self.route.strip("/"),
                       "model": self.model, "version": self.version, "dimensions": self.dimensions})


class _HttpProvider:
    kind = "provider"

    def __init__(self, config: ProviderConfig, *, client: httpx.AsyncClient | None = None):
        self.config, self.client = config, client

    async def _post(self, payload):
        headers = {"Authorization": f"Bearer {self.config.api_key}"} if self.config.api_key else {}
        try:
            if self.client is not None:
                response = await self.client.post(self.config.url, json=payload, headers=headers,
                                                  timeout=self.config.timeout)
            else:
                async with httpx.AsyncClient(timeout=self.config.timeout) as client:
                    response = await client.post(self.config.url, json=payload, headers=headers)
            response.raise_for_status()
            result = response.json()
        except httpx.TimeoutException as exc:
            raise ProviderUnavailable(self.kind, "timeout") from exc
        except httpx.HTTPStatusError as exc:
            raise ProviderUnavailable(self.kind, f"http_{exc.response.status_code}") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderUnavailable(self.kind, "invalid_response_or_transport") from exc
        if not isinstance(result, dict):
            raise ProviderUnavailable(self.kind, "invalid_response_shape")
        if result.get("model") is not None and result["model"] != self.config.model:
            raise ProviderUnavailable(self.kind, "response_model_mismatch")
        return result


class EmbeddingProvider(_HttpProvider):
    kind = "embedding"

    def __init__(self, config, *, client=None):
        super().__init__(config, client=client)
        self._dimension = config.dimensions

    def _check_config(self, config):
        get = config.get if isinstance(config, dict) else lambda key, default=None: getattr(config, key, default)
        model = get("embedding_model", get("model"))
        version = get("embedding_version", get("version"))
        endpoint = get("embedding_endpoint_hash", get("endpoint_hash"))
        if model != self.config.model or version != self.config.version or endpoint != self.config.endpoint_hash:
            raise ProviderUnavailable(self.kind, "snapshot_configuration_mismatch")

    async def embed(self, text: str, config) -> list[float]:
        self._check_config(config)
        if not isinstance(text, str) or not text.strip():
            raise ProviderUnavailable(self.kind, "empty_input")
        payload: dict[str, Any] = {"model": self.config.model, "input": text, "encoding_format": "float"}
        if self.config.dimensions is not None:
            payload["dimensions"] = self.config.dimensions
        data = await self._post(payload)
        items = data.get("data")
        if (not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict) or
                not isinstance(items[0].get("index"), int) or isinstance(items[0].get("index"), bool) or
                items[0]["index"] != 0):
            raise ProviderUnavailable(self.kind, "invalid_embedding_indices")
        raw = items[0].get("embedding")
        if (not isinstance(raw, list) or not raw or
                any(not isinstance(item, int | float) or isinstance(item, bool) for item in raw)):
            raise ProviderUnavailable(self.kind, "invalid_vector")
        vector = [float(item) for item in raw]
        if not all(math.isfinite(item) for item in vector) or not any(vector):
            raise ProviderUnavailable(self.kind, "invalid_vector")
        if self._dimension is not None and len(vector) != self._dimension:
            raise ProviderUnavailable(self.kind, "dimension_mismatch")
        self._dimension = len(vector)
        return vector

    async def embed_query(self, text: str, config) -> list[float]:
        return await self.embed(text, config)


class HttpReranker(_HttpProvider):
    kind = "reranker"

    async def rerank(self, query: str, chunks: list[dict]) -> list[dict]:
        if not query.strip() or not chunks:
            raise ProviderUnavailable(self.kind, "empty_input")
        ids = [chunk.get("chunk_id") for chunk in chunks]
        if any(not isinstance(key, str) or not key for key in ids) or len(set(ids)) != len(ids):
            raise ProviderUnavailable(self.kind, "invalid_chunk_ids")
        data = await self._post({"model": self.config.model, "query": query,
                                 "documents": [chunk["text"] for chunk in chunks],
                                 "top_n": len(chunks), "return_documents": False})
        results = data.get("results", data.get("data"))
        if not isinstance(results, list) or len(results) != len(chunks):
            raise ProviderUnavailable(self.kind, "incomplete_rerank_results")
        seen, ranked = set(), []
        for item in results:
            if not isinstance(item, dict):
                raise ProviderUnavailable(self.kind, "invalid_rerank_result")
            index, score = item.get("index"), item.get("relevance_score", item.get("score"))
            if (not isinstance(index, int) or isinstance(index, bool) or
                    not 0 <= index < len(chunks) or index in seen):
                raise ProviderUnavailable(self.kind, "invalid_rerank_indices")
            if (not isinstance(score, int | float) or isinstance(score, bool) or
                    not math.isfinite(score)):
                raise ProviderUnavailable(self.kind, "invalid_rerank_score")
            seen.add(index)
            ranked.append({"chunk_id": chunks[index]["chunk_id"], "score": float(score)})
        return ranked


@dataclass
class AssistantModelProviders:
    embedding: EmbeddingProvider | None
    reranker: HttpReranker | None
    service_states: dict

    def index_config(self, base: IndexConfig | None = None) -> IndexConfig:
        config = base or IndexConfig()
        emb, rank = self.embedding, self.reranker
        return replace(config,
            embedding_model=emb.config.model if emb else None,
            embedding_version=emb.config.version if emb else None,
            embedding_endpoint_hash=emb.config.endpoint_hash if emb else None,
            reranker=rank.config.model if rank else None,
            reranker_model=rank.config.model if rank else None,
            reranker_version=rank.config.version if rank else None,
            reranker_endpoint_hash=rank.config.endpoint_hash if rank else None)


def providers_from_settings(settings, *, client=None) -> AssistantModelProviders:
    models = settings.get("models", {}) if isinstance(settings, dict) else getattr(settings, "models", {})
    providers: dict[str, Any] = {}
    states: dict[str, dict] = {}
    for kind, cls, route in (("embedding", EmbeddingProvider, "/embeddings"),
                             ("reranker", HttpReranker, "/rerank")):
        raw = models.get(kind) if isinstance(models, dict) else None
        if raw is None:
            providers[kind], states[kind] = None, {"status": "unavailable", "reason": "unconfigured"}
            continue
        if isinstance(raw, dict):
            def get(key, default=None, _raw=raw):
                return _raw.get(key, default)
        else:
            def get(key, default=None, _raw=raw):
                return getattr(_raw, key, default)
        try:
            config = ProviderConfig(base_url=get("base_url", ""), model=get("model_name", get("model", "")),
                                    version=get("model_version", get("version", "unknown")),
                                    route=get("route") or route, api_key=get("api_key"),
                                    timeout=float(get("timeout", 60)), dimensions=get("dimensions"))
            providers[kind] = cls(config, client=client)
            states[kind] = {"status": "configured", "model": config.model,
                            "version": config.version, "endpoint_hash": config.endpoint_hash}
        except (TypeError, ValueError):
            providers[kind], states[kind] = None, {"status": "unavailable", "reason": "invalid_configuration"}
    return AssistantModelProviders(providers["embedding"], providers["reranker"], states)
