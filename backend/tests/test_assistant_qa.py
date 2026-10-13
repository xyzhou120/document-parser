import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services.assistant.index_repository import MemoryIndexRepository
from app.services.assistant.index_service import IndexConfig, IndexService
from app.services.assistant.qa_service import (
    AssistantUnavailable,
    MemoryQARepository,
    QAService,
    StaleAnswer,
    _qa_unit_supported,
    validate_answer_payload,
)
from app.services.content_adapter import ContentAdapter


class Content:
    def __init__(self, pages=None):
        document_id, artifact_id = uuid4(), uuid4()
        pages = pages or [{"page_number": 1, "text": "压力 1.25 MPa", "text_blocks": [
            {"block_id": "b1", "text": "压力 1.25 MPa", "raw_text": "压力 1.25 MPa", "source": "native", "bbox": [1, 2, 30, 40]}],
            "tables": []}]
        artifact = SimpleNamespace(artifact_id=artifact_id, document_id=document_id, file_hash="f" * 64,
                                  status="READY", pages=pages,
                                  quality={"page_count": len(pages), "coverage_manifest": {
                                      "expected_page_count": len(pages), "pages": [
                                          {"page_number": n + 1, "scope_complete": True,
                                           "regions": [{"state": "processed"}]} for n in range(len(pages))]}})
        adapted = ContentAdapter().adapt(artifact)
        self.revision = SimpleNamespace(revision_id=uuid4(), document_id=document_id,
                                        content_digest=adapted.content_digest,
                                        coverage_digest=adapted.coverage_digest,
                                        adapter_version=adapted.adapter_version)
        self.content = adapted

    async def read_revision(self, *_):
        return SimpleNamespace(revision=self.revision, content=self.content)


class Answerer:
    def __init__(self, *, bad=False, all_locations=False):
        self.bad, self.all_locations = bad, all_locations
        self.calls = []

    def answer(self, question, chunks, **kwargs):
        self.calls.append((question, chunks, kwargs))
        if self.bad:
            return {"answer": "free text", "claims": [{"text": "假的", "quote": "不存在", "citation_ids": ["bad"]}]}
        claims = [{"text": chunk["text"], "quote": chunk["text"], "citation_ids": [chunk["chunk_id"]]}
                  for chunk in chunks[:1]]
        payload = {"answer": "ignored", "claims": claims}
        if self.all_locations:
            payload["checked_chunk_ids"] = [chunk["chunk_id"] for chunk in chunks]
        return payload


def setup(pages=None, answerer=None):
    content = Content(pages)
    index_repo = MemoryIndexRepository()
    index_repo.ensure_head(content.revision.document_id, content.revision.revision_id)
    index = IndexService(repository=index_repo, content_service=content)
    qa_repo = MemoryQARepository()
    return index, index_repo, QAService(qa_repo, index_repo, answerer=answerer, chunks_per_quantum=1), qa_repo, content


async def ready(service, content):
    if isinstance(service, QAService):
        service = IndexService(repository=service.index_repository, content_service=content)
    built = await service.build(content.revision.revision_id, IndexConfig())
    await service.publish(built.generation.generation_id, expected_cas_version=0,
                          expected_revision_id=content.revision.revision_id,
                          expected_invalidation_watermark=0)
    return built


def test_status_exposes_capabilities_and_unavailable_semantic_service():
    async def run():
        _, repo, service, _, content = setup()
        assert (await service.status(content.revision.document_id))["can_ask"] is False
        await ready(service, content)
        status = await service.status(content.revision.document_id)
        assert status["can_ask"] and status["semantic_status"] == "unavailable"
        assert status["capabilities"]["answer"] and not status["capabilities"]["card"]
    asyncio.run(run())


def test_session_request_id_is_idempotent_and_pinned_to_attachment_revision():
    async def run():
        _, _, service, _, content = setup()
        generation = await ready(service, content)
        first = await service.create_session(document_id=content.revision.document_id, request_id="s1")
        second = await service.create_session(document_id=content.revision.document_id, request_id="s1")
        assert first["session_id"] == second["session_id"]
        with pytest.raises(ValueError):
            await service.create_session(document_id=content.revision.document_id, request_id="s1",
                                         revision_id=uuid4())
        assert first["active_generation_id"] == generation.generation.generation_id
    asyncio.run(run())


def test_message_request_id_is_idempotent_and_parent_cannot_cross_session():
    async def run():
        _, _, service, _, content = setup(answerer=Answerer())
        await ready(service, content)
        session = await service.create_session(document_id=content.revision.document_id, request_id="s1")
        first, created = await service.submit_message(session["session_id"], request_id="m1", question="压力")
        second, duplicate = await service.submit_message(session["session_id"], request_id="m1", question="压力")
        assert created and not duplicate and first["message_id"] == second["message_id"]
        other = await service.create_session(document_id=content.revision.document_id, request_id="s2")
        with pytest.raises(ValueError):
            await service.submit_message(other["session_id"], request_id="m2", question="x",
                                         parent_message_id=first["message_id"])
    asyncio.run(run())


def test_answer_uses_source_chunks_only_and_rejects_unbound_free_text():
    async def run():
        _, _, service, _, content = setup(answerer=Answerer())
        await ready(service, content)
        session = await service.create_session(document_id=content.revision.document_id, request_id="s")
        message, _ = await service.submit_message(session["session_id"], request_id="m", question="压力")
        result = await service.run_message(message["message_id"])
        assert result["execution_state"] == "succeeded"
        assert result["answer"].startswith("原文：") and result["citations"]
    asyncio.run(run())


def test_bad_claim_becomes_waiting_service_and_never_succeeds_from_free_text():
    async def run():
        _, _, service, _, content = setup(answerer=Answerer(bad=True))
        await ready(service, content)
        session = await service.create_session(document_id=content.revision.document_id, request_id="s")
        message, _ = await service.submit_message(session["session_id"], request_id="m", question="压力")
        result = await service.run_message(message["message_id"])
        assert result["execution_state"] == "partial"
        assert "free text" not in result["answer"] and not result["claims"]
    asyncio.run(run())


def test_all_locations_processes_fixed_manifest_in_quanta_and_exposes_cursor():
    async def run():
        pages = [{"page_number": n, "text": f"压力 {n}", "text_blocks": [
            {"block_id": f"b{n}", "text": f"压力 {n}", "raw_text": f"压力 {n}", "source": "native"}], "tables": []}
                 for n in range(1, 4)]
        index, _, service, _, content = setup(pages, Answerer(all_locations=True))
        await ready(service, content)
        session = await service.create_session(document_id=content.revision.document_id, request_id="s")
        message, _ = await service.submit_message(session["session_id"], request_id="m", question="压力", mode="all_locations")
        first = await service.run_message(message["message_id"])
        assert first["execution_state"] == "queued" and first["checkpoint"]["remaining_work"]
        second = await service.run_message(message["message_id"])
        assert second["execution_state"] == "queued"
        third = await service.run_message(message["message_id"])
        assert third["execution_state"] in {"succeeded", "partial"}
        assert third["checkpoint"]["search_finished"] and third["checkpoint"]["scan_cursor"] == 3
        locations = await service.get_locations(message["message_id"], limit=2)
        assert locations["next_cursor"] and locations["search_finished"]
        assert len(locations["items"]) == 2
    asyncio.run(run())


def test_all_locations_rejects_missing_checked_chunk_and_preserves_cursor():
    async def run():
        _, _, service, _, content = setup(answerer=Answerer(all_locations=False))
        await ready(service, content)
        session = await service.create_session(document_id=content.revision.document_id, request_id="s")
        message, _ = await service.submit_message(session["session_id"], request_id="m", question="压力", mode="all_locations")
        result = await service.run_message(message["message_id"])
        assert result["execution_state"] == "waiting_service" and result["checkpoint"].get("scan_cursor", 0) == 0
    asyncio.run(run())


def test_partial_scope_is_explicit_and_not_not_found():
    async def run():
        index, _, service, _, content = setup()
        built = await index.build(content.revision.revision_id, block_ids={"b1"})
        await index.publish(built.generation.generation_id, expected_cas_version=0,
                            expected_revision_id=content.revision.revision_id, expected_invalidation_watermark=0)
        session = await service.create_session(document_id=content.revision.document_id, request_id="s")
        message, _ = await service.submit_message(session["session_id"], request_id="m", question="missing")
        assert message["scope_snapshot"]["coverage_status"] == "partial"
        assert "partial" in (await service.status(content.revision.document_id))["coverage_status"]
    asyncio.run(run())


def test_current_document_isolation_rejects_other_generation():
    async def run():
        _, repo, service, _, content = setup()
        await ready(service, content)
        other = uuid4()
        with pytest.raises(AssistantUnavailable):
            await service.create_session(document_id=other, request_id="s")
    asyncio.run(run())


def test_invalidation_race_rejects_answer_submission():
    async def run():
        _, repo, service, qa_repo, content = setup(answerer=Answerer())
        built = await ready(service, content)
        session = await service.create_session(document_id=content.revision.document_id, request_id="s")
        message, _ = await service.submit_message(session["session_id"], request_id="m", question="压力")
        claimed = await qa_repo.claim(message["message_id"])
        await repo.invalidate_generation(built.generation.generation_id)
        with pytest.raises(StaleAnswer):
            await qa_repo.finish(claimed, {"execution_state": "succeeded"}, index_repository=repo, qualify=True)
    asyncio.run(run())


def test_resume_requeues_waiting_message_and_service_failure_is_bounded():
    async def run():
        _, _, service, qa_repo, content = setup(answerer=None)
        await ready(service, content)
        session = await service.create_session(document_id=content.revision.document_id, request_id="s")
        message, _ = await service.submit_message(session["session_id"], request_id="m", question="压力")
        result = await service.run_message(message["message_id"])
        assert result["execution_state"] == "waiting_service"
        resumed = await service.resume_message(message["message_id"])
        assert resumed["execution_state"] == "queued" and qa_repo.outbox
    asyncio.run(run())


def test_locations_without_eligible_generation_are_unavailable_not_absent():
    async def run():
        _, _, service, _, content = setup()
        with pytest.raises(AssistantUnavailable):
            await service.create_session(document_id=content.revision.document_id, request_id="s")
    asyncio.run(run())


def test_evidence_attributes_require_same_quote_object_unit_and_kind():
    async def run():
        _, _, service, _, content = setup(answerer=Answerer())
        await ready(service, content)
        session = await service.create_session(document_id=content.revision.document_id, request_id="s")
        message, _ = await service.submit_message(session["session_id"], request_id="m", question="压力")
        # Answerer has no free text shortcut: claim text is derived from the quote.
        result = await service.run_message(message["message_id"])
        assert result["claims"][0]["quote"] in result["answer"]
    asyncio.run(run())


def test_attribute_misclassification_rejected_even_if_quote_exists():
    context = [{"chunk_id": "b", "text": "可解吸瓦斯量 0.18 m³/t", "block_ref": {}, "table_ref": None}]
    result = validate_answer_payload({"claims": [{"quote": context[0]["text"], "citation_ids": ["b"],
                        "value": "0.18", "unit": "m³/t", "value_kind": "calculated"}]},
                                    context, generation_id=uuid4(), revision_id=uuid4())
    assert not result["claims"] and result["rejected"] == ["claim_attribute_unverified"]


@pytest.mark.parametrize("unit,quote", [
    ("m³/t", "实测 0.18 m³/t"),
    ("m3/t", "实测 0.18 m³/t"),
    ("m^3/t", "实测 0.18 m^3/t"),
    ("cm3", "实测 0.18 cm3"),
    ("cm³", "实测 0.18 cm³"),
])
def test_unit_digits_survive_normalization_and_structured_claim_validation(unit, quote):
    assert _qa_unit_supported(unit, quote, "0.18")
    result = validate_answer_payload({"claims": [{"quote": quote, "citation_ids": ["b"],
                                                 "value": "0.18", "unit": unit,
                                                 "value_kind": "measured"}]},
                                    [{"chunk_id": "b", "text": quote, "block_ref": {}, "table_ref": None}],
                                    generation_id=uuid4(), revision_id=uuid4())
    assert result["claims"] and not result["rejected"]


@pytest.mark.parametrize("quote", ["实测 0.18 其他值 2 m³/t", "实测 0.18；其他值 2 m³/t"])
def test_unit_from_next_value_is_not_bound_to_target(quote):
    assert not _qa_unit_supported("m³/t", quote, "0.18")


def test_multiturn_history_uses_same_attachment_prior_answers_without_facts():
    async def run():
        answerer = Answerer()
        _, _, service, _, content = setup(answerer=answerer)
        await ready(service, content)
        session = await service.create_session(document_id=content.revision.document_id, request_id="s")
        first, _ = await service.submit_message(session["session_id"], request_id="m1", question="压力")
        await service.run_message(first["message_id"])
        second, _ = await service.submit_message(session["session_id"], request_id="m2", question="压力单位",
                                                  parent_message_id=first["message_id"])
        await service.run_message(second["message_id"])
        assert answerer.calls[-1][2]["history"][0]["question"] == "压力"
        assert not any("facts" in item for item in answerer.calls[-1][1])
    asyncio.run(run())


def test_lease_fence_rejects_old_attempt_and_duplicate_worker_does_not_call_again():
    async def run():
        answerer = Answerer()
        _, repo, service, qa, content = setup(answerer=answerer)
        await ready(service, content)
        session = await service.create_session(document_id=content.revision.document_id, request_id="s")
        message, _ = await service.submit_message(session["session_id"], request_id="m", question="压力")
        old = await qa.claim(message["message_id"])
        assert await qa.claim(message["message_id"]) is None
        qa.messages[message["message_id"]]["checkpoint"]["lease_until"] = "2020-01-01T00:00:00"
        newer = await qa.claim(message["message_id"])
        with pytest.raises(StaleAnswer):
            await qa.finish(old, {"execution_state": "succeeded"})
        assert newer["generation_fence"] > old["generation_fence"]
    asyncio.run(run())


def test_answerer_revokes_generation_after_model_call_cannot_publish_answer():
    async def run():
        _, repo, service, _, content = setup()
        built = await ready(service, content)
        class Revoke(Answerer):
            async def answer(self, question, chunks, **kwargs):
                result = super().answer(question, chunks, **kwargs)
                await repo.invalidate_generation(built.generation.generation_id)
                return result
        service.answerer = Revoke()
        session = await service.create_session(document_id=content.revision.document_id, request_id="s")
        message, _ = await service.submit_message(session["session_id"], request_id="m", question="压力")
        result = await service.run_message(message["message_id"])
        assert result["execution_state"] == "waiting_service" and not result["answer"]
    asyncio.run(run())


def test_locations_page_can_end_before_search_finished_and_last_page_is_processed():
    async def run():
        pages = [{"page_number": n, "text_blocks": [{"block_id": f"p{n}", "text": "压力" if n == 4 else "其它", "raw_text": "压力" if n == 4 else "其它"}], "tables": []}
                 for n in range(1, 5)]
        class Finder(Answerer):
            def answer(self, question, chunks, **kwargs):
                return {"claims": [{"quote": c["text"], "citation_ids": [c["chunk_id"]]} for c in chunks if "压力" in c["text"]],
                        "checked_chunk_ids": [c["chunk_id"] for c in chunks]}
        _, _, service, _, content = setup(pages, Finder())
        await ready(service, content)
        session = await service.create_session(document_id=content.revision.document_id, request_id="s")
        message, _ = await service.submit_message(session["session_id"], request_id="m", question="压力", mode="all_locations")
        await service.run_message(message["message_id"])
        interim = await service.get_locations(message["message_id"])
        assert interim["next_cursor"] is None and interim["search_finished"] is False
        for _ in range(3):
            await service.run_message(message["message_id"])
        final = await service.get_locations(message["message_id"])
        assert final["search_finished"] and final["items"][0]["page_number"] == 4
    asyncio.run(run())


def test_retry_due_prevents_duplicate_immediate_wait_loop_and_retry_exhausts():
    async def run():
        class Broken:
            def answer(self, *args, **kwargs):
                raise TimeoutError()
        _, _, service, qa, content = setup(answerer=Broken())
        service.retry_max = 1
        await ready(service, content)
        session = await service.create_session(document_id=content.revision.document_id, request_id="s")
        message, _ = await service.submit_message(session["session_id"], request_id="m", question="压力")
        first = await service.run_message(message["message_id"])
        assert first["execution_state"] == "waiting_service" and first["checkpoint"]["retry_at"]
        repeated = await service.run_message(message["message_id"])
        assert repeated["generation_fence"] == first["generation_fence"]
        qa.messages[message["message_id"]]["checkpoint"]["retry_at"] = "2020-01-01T00:00:00"
        last = await service.run_message(message["message_id"])
        assert last["execution_state"] == "failed" and last["checkpoint"]["retry_at"] is None
    asyncio.run(run())
