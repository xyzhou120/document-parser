"""Resumable all-location search over one immutable index generation."""

from __future__ import annotations

from dataclasses import dataclass

from app.services.assistant.index_repository import digest
from app.services.assistant.retrieval_service import IndexUnavailable, terms


@dataclass
class LocationResult:
    hits: list[dict]
    next_cursor: str | None
    search_finished: bool
    searched: int
    total_matches: int | None


class LocationSearch:
    def __init__(self, repository):
        self.repository = repository

    async def search(self, generation_id, query: str, *, cursor: str | None = None,
                     page_size: int = 20) -> LocationResult:
        if not query.strip() or page_size < 1 or page_size > 200:
            raise ValueError("query and page_size are invalid")
        generation = await self.repository.get_generation(generation_id)
        if generation is None or generation.serve_status != "eligible" or generation.coverage_status not in {"partial", "full"}:
            raise IndexUnavailable("generation unavailable")
        if not await self.repository.is_generation_current(
                generation.generation_id, revision_id=generation.revision_id,
                document_id=generation.document_id,
                invalidation_watermark=generation.invalidation_watermark):
            raise IndexUnavailable("generation changed or invalidated")
        try:
            start = int(cursor or 0)
        except (TypeError, ValueError):
            raise ValueError("cursor is invalid") from None
        if start < 0 or start > len(generation.chunks):
            raise ValueError("cursor is outside generation")
        chunks = generation.chunks
        manifest_hashes = {item["chunk_id"]: item["digest"]
                           for item in generation.indexed_block_manifest.get("chunks", [])}
        if {chunk["chunk_id"] for chunk in chunks} != set(manifest_hashes) or any(
                digest({**chunk, "embedding": None, "embedding_config": None}) != manifest_hashes[chunk["chunk_id"]]
                for chunk in chunks):
            raise IndexUnavailable("generation raw chunks differ from manifest")
        query_terms = terms(query)
        # Cursor is over a fixed manifest, not over the top-k result list. This
        # prevents a moving finite top-k from pretending all locations were searched.
        matches = []
        end = min(len(chunks), start + page_size)
        for chunk in chunks[start:end]:
            if query_terms & terms(chunk["text"]):
                matches.append({**chunk, "generation_id": str(generation.generation_id),
                                "revision_id": str(generation.revision_id),
                                "citation": {"chunk_id": chunk["chunk_id"],
                                             "block_ref": chunk.get("block_ref"),
                                             "table_ref": chunk.get("table_ref")}})
        done = end >= len(chunks)
        return LocationResult(matches, None if done else str(end), done, end,
                              len(matches) if done else None)
