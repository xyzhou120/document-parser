"""使用确定性提供方和隔离 PG 验证真实的 P06/P07/P10 编排。"""

import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.models.database import Document, DocumentArtifact
from app.rules.spec import RuleSpec
from app.services.call_ledger import CallLedger
from app.services.model_gateway import ModelGateway
from app.services.reliable_extraction_service import (
    ContentPreparationRequired,
    ReliableExtractionService,
)
from app.services.reliable_field_executor import ReliableFieldExecutor, ReliableFieldExecutorConfig
from app.services.service_circuit import MemoryCircuitStore, ServiceCircuit
from tests.test_field_scheduler_postgres import sessions as pg_sessions

sessions = pg_sessions


class FakeProvider:
    def __init__(self):
        self.calls = 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self, **kwargs):
        self.calls += 1
        prompt = json.loads(kwargs["messages"][-1]["content"][0]["text"])
        decisions = []
        for item in prompt["candidates"]:
            if "瓦斯压力" in item["text"]:
                decisions.append({"candidate_id": item["candidate_id"], "judgement": "accepted",
                    "value": "1.23", "unit": "MPa", "value_kind": "measured", "source_role": item.get("source_role_hint"),
                    "evidence_ids": item.get("evidence_ids", []), "dimensions": {}})
            else:
                decisions.append({"candidate_id": item["candidate_id"], "judgement": "rejected"})
        return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"decisions": decisions})}}],
                "usage": {"prompt_tokens": 40, "completion_tokens": 20, "total_tokens": 60}}


async def make_run(sessions, count=1, *, present=True, covered=True, text_override=None, rules_override=None,
                   source="native", extraction_strategy="field_retrieval_v2"):
    text = text_override or ("报告正文 瓦斯压力 1.23MPa 实测" if present else "目录与说明。本附件无相关数据。")
    if present and "报告正文" not in text:
        text = "报告正文 " + text
    document_id = uuid4()
    pages = [{"page_number": 1, "width": 600, "height": 800, "text": text, "native_text": text,
              "ocr_status": "not_needed" if source == "native" else "ok", "text_blocks": [{"text": text, "source": source,
                                                          "bbox": [0, 0, 500, 40], "block_id": "synthetic-1",
                                                          "coordinate_system": "PDF_POINT",
                                                          "start_offset": 0, "end_offset": len(text)}]}]
    quality = {"coverage_manifest": {"expected_page_count": 1, "pages": [
        {"page_number": 1, "scope_complete": True, "regions": [{"state": "processed", "whole_page": True}]}]}}
    async with sessions() as db, db.begin():
        db.add(Document(document_id=document_id, file_name="synthetic.pdf", original_name="synthetic.pdf",
                        file_path="test/p13", file_size=1, file_hash="e" * 64, mime_type="application/pdf"))
        await db.flush()
        artifact = DocumentArtifact(document_id=document_id, file_hash="e" * 64,
                                    preprocess_profile_version="synthetic-v2", status="READY", pages=pages,
                                    quality=quality if covered else {})
        db.add(artifact)
        await db.flush()
        rules = rules_override or [RuleSpec(field_key=f"pressure{i}", field_name_cn="瓦斯压力", data_type="float", unit="MPa",
                          value_kind="measured") for i in range(count)]
        try:
            run = await ReliableExtractionService().start(db, document_id, rules=rules,
                                                         extraction_strategy=extraction_strategy)
        except ContentPreparationRequired as exc:
            return exc
        return run.parse_run_id


@pytest.mark.asyncio
@pytest.mark.parametrize("rule_count", [1, 10, 30, 100])
async def test_real_executor_full_range_closes_all_rules(sessions, rule_count):
    run_id = await make_run(sessions, rule_count, present=False)
    provider = FakeProvider()
    gateway = ModelGateway(provider, CallLedger(sessions), ServiceCircuit(MemoryCircuitStore()))
    executor = ReliableFieldExecutor(sessions, gateway, ReliableFieldExecutorConfig("test", "test"))
    service = ReliableExtractionService()
    result = None
    for _ in range(30):
        result = await service.process_run(sessions, run_id, executor=executor, writer=executor.write_result,
            before_publish=executor.before_publish, owner="fake-provider", max_quantums=50, lease_seconds=300)
        if result["status"] == "COMPLETED":
            break
    assert result["status"] == "COMPLETED"
    async with sessions() as db:
        summary = await service.execution(db, run_id, limit=500)
    assert summary["total_fields"] == rule_count
    assert summary["counts"]["semantic:not_found"] == rule_count


@pytest.mark.asyncio
@pytest.mark.parametrize("second_value", ["2.34", "1.23"])
@pytest.mark.parametrize("parent_evidence", [False, True])
async def test_multiple_objects_in_one_candidate_preserve_both_facts_and_receipts(sessions, second_value, parent_evidence):
    from sqlalchemy import select

    from app.models.database import ExtractedFieldModel

    class MultiProvider(FakeProvider):
        async def create(self, **kwargs):
            self.calls += 1
            prompt = json.loads(kwargs["messages"][-1]["content"][0]["text"])
            decisions = []
            for item in prompt["candidates"]:
                facts = [{"value": value, "unit": "MPa", "value_kind": "measured",
                          "source_role": item.get("source_role_hint"), "dimensions": {"sample": identity},
                          "evidence_ids": item["evidence_ids"]}
                         for value, identity in [("1.23", "样品A"), (second_value, "样品B")]]
                decision = {"candidate_id": item["candidate_id"], "judgement": "accepted", "facts": facts}
                if parent_evidence:
                    for fact in facts:
                        fact.pop("evidence_ids")
                    decision["evidence_ids"] = item["evidence_ids"]
                decisions.append(decision)
            return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"decisions": decisions})}}],
                    "usage": {"prompt_tokens": 40, "completion_tokens": 20, "total_tokens": 60}}

    rule = RuleSpec(field_key="pressure", field_name_cn="瓦斯压力", data_type="float", unit="MPa",
                    value_kind="measured", multi_instance=True, dimensions=["sample"],
                    dimension_schema={"sample": {"required": True}}, strategy={"semantic_contract": {
                        "cardinality": "multi_identity", "identity_dimensions": ["sample"],
                        "applicable_dimensions": ["sample"]}})
    run_id = await make_run(sessions, text_override=f"样品A 实测瓦斯压力 1.23MPa；样品B 实测瓦斯压力 {second_value}MPa",
                             rules_override=[rule])
    provider = MultiProvider()
    executor = ReliableFieldExecutor(sessions,
        ModelGateway(provider, CallLedger(sessions), ServiceCircuit(MemoryCircuitStore())),
        ReliableFieldExecutorConfig("test", "test"))
    result = await ReliableExtractionService().process_run(sessions, run_id, executor=executor,
        writer=executor.write_result, before_publish=executor.before_publish,
        owner="multi", max_quantums=50, lease_seconds=300)
    assert result["status"] == "COMPLETED"
    async with sessions() as db:
        field = await db.scalar(select(ExtractedFieldModel).where(ExtractedFieldModel.parse_run_id == run_id))
        assert {fact["value"] for fact in field.instances} == {"1.23", second_value}
        assert len(field.instances) == 2
        assert {fact["dimensions"]["sample"] for fact in field.instances} == {"样品A", "样品B"}
        assert field.extraction_detail["evidence_registry"]


@pytest.mark.asyncio
async def test_unknown_content_coverage_never_becomes_not_found(sessions):
    build = await make_run(sessions, present=False, covered=False)
    assert isinstance(build, ContentPreparationRequired)
    from app.models.content import ContentBuildJob
    async with sessions() as db:
        stored = await db.get(ContentBuildJob, build.build_id)
        assert stored.dispatch_state == "queued"
        assert stored.checkpoint.get("parse_run_id") is None


@pytest.mark.asyncio
async def test_present_candidate_uses_gateway_and_validated_writer(sessions):
    run_id = await make_run(sessions, 1, present=True)
    provider = FakeProvider()
    gateway = ModelGateway(provider, CallLedger(sessions), ServiceCircuit(MemoryCircuitStore()))
    executor = ReliableFieldExecutor(sessions, gateway, ReliableFieldExecutorConfig("test", "test"))
    service = ReliableExtractionService()
    result = None
    for _ in range(20):
        result = await service.process_run(sessions, run_id, executor=executor, writer=executor.write_result,
            before_publish=executor.before_publish, owner="fake-present", max_quantums=50, lease_seconds=300)
        if result["status"] in {"COMPLETED", "FAILED"}:
            break
    assert provider.calls > 0
    assert result["status"] == "COMPLETED"


@pytest.mark.asyncio
async def test_length_response_splits_candidates_and_finishes_without_manual_resume(sessions):
    from sqlalchemy import select

    from app.models.execution import ParseCandidate, ParseFieldJob

    class LengthProvider(FakeProvider):
        split_requests = 0

        async def create(self, **kwargs):
            prompt = json.loads(kwargs["messages"][-1]["content"][0]["text"])
            if len(prompt["candidates"]) > 1:
                self.calls += 1
                self.split_requests += 1
                return {"choices": [{"finish_reason": "length", "message": {"content": '{"decisions":['}}],
                        "usage": {"prompt_tokens": 40, "completion_tokens": 20, "total_tokens": 60}}
            return await super().create(**kwargs)

    run_id = await make_run(sessions, text_override=("瓦斯压力 1.23MPa 实测 " * 40))
    provider = LengthProvider()
    executor = ReliableFieldExecutor(sessions,
        ModelGateway(provider, CallLedger(sessions), ServiceCircuit(MemoryCircuitStore())),
        ReliableFieldExecutorConfig("test", "test", window_chars=80))
    service = ReliableExtractionService()
    result = None
    for _ in range(10):
        result = await service.process_run(sessions, run_id, executor=executor, writer=executor.write_result,
            before_publish=executor.before_publish, owner="length", max_quantums=50, lease_seconds=300)
        if result["status"] in {"COMPLETED", "FAILED"}:
            break
    assert result["status"] == "COMPLETED"
    assert provider.split_requests > 0
    async with sessions() as db:
        candidates = list((await db.execute(select(ParseCandidate).join(
            ParseFieldJob, ParseFieldJob.job_id == ParseCandidate.job_id).where(
                ParseFieldJob.run_id == run_id))).scalars().all())
    assert len(candidates) > 2
    assert all(candidate.state in {"accepted", "rejected"} for candidate in candidates)
    accepted = [candidate for candidate in candidates if candidate.state == "accepted"]
    assert len(accepted) > 2
    assert all(any(receipt.get("attributes", {}).get("source_role", {}).get("value") == "report_body"
                   for receipt in candidate.evidence.get("registry", {}).values())
               for candidate in accepted)


@pytest.mark.asyncio
async def test_external_quota_resume_creates_new_call_lineage_and_completes(sessions):
    class QuotaError(Exception):
        status_code = 429
        body = {"error": {"code": "insufficient_quota"}}

    class QuotaProvider(FakeProvider):
        async def create(self, **kwargs):
            if self.calls == 0:
                self.calls += 1
                raise QuotaError("external quota exhausted")
            return await super().create(**kwargs)

    run_id = await make_run(sessions, present=True)
    ledger = CallLedger(sessions)
    executor = ReliableFieldExecutor(sessions,
        ModelGateway(QuotaProvider(), ledger, ServiceCircuit(MemoryCircuitStore())),
        ReliableFieldExecutorConfig("test", "test"))
    service = ReliableExtractionService()
    result = await service.process_run(sessions, run_id, executor=executor, writer=executor.write_result,
        before_publish=executor.before_publish, owner="quota", max_quantums=50, lease_seconds=300)
    assert result["status"] == "WAITING_EXTERNAL_QUOTA"
    async with sessions() as db, db.begin():
        await service.control(db, run_id, "resume")
    result = await service.process_run(sessions, run_id, executor=executor, writer=executor.write_result,
        before_publish=executor.before_publish, owner="quota-resumed", max_quantums=50, lease_seconds=300)
    assert result["status"] == "COMPLETED"


@pytest.mark.asyncio
async def test_ocr_requires_separate_visual_transcription_then_p10_receipt(sessions):
    import hashlib

    class ImageProvider(FakeProvider):
        async def create(self, **kwargs):
            prompt = json.loads(kwargs["messages"][-1]["content"][0]["text"])
            is_visual = any(item.get("stage") == "original_image_transcription" for item in prompt["candidates"])
            response = await super().create(**kwargs)
            if is_visual:
                assert prompt["field_rule"]["field_key"] == "pressure0"
                data = json.loads(response["choices"][0]["message"]["content"])
                for item, proposal in zip(data["decisions"], prompt["candidates"], strict=True):
                    item["transcription"] = proposal["text"]
                    item["source_role"] = "report_body"
                response["choices"][0]["message"]["content"] = json.dumps(data)
            return response

    image = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + (1).to_bytes(4, "big") * 2
    async def render(document, pages, candidate):
        return image, {"image_hash": hashlib.sha256(image).hexdigest(), "page_number": 1,
                       "bbox": [0, 0, 500, 40], "source_file_hash": document.file_hash}

    run_id = await make_run(sessions, source="ocr")
    provider = ImageProvider()
    executor = ReliableFieldExecutor(sessions,
        ModelGateway(provider, CallLedger(sessions), ServiceCircuit(MemoryCircuitStore())),
        ReliableFieldExecutorConfig("test", "test"), image_provider=render)
    service = ReliableExtractionService()
    result = None
    for _ in range(5):
        result = await service.process_run(sessions, run_id, executor=executor, writer=executor.write_result,
            before_publish=executor.before_publish, owner="ocr", max_quantums=50, lease_seconds=300)
        if result["status"] in {"COMPLETED", "FAILED"}:
            break
    assert result["status"] == "COMPLETED"
    assert provider.calls >= 2


@pytest.mark.asyncio
async def test_ocr_mismatch_publishes_successor_and_recomputes_all_fields(sessions):
    import hashlib

    from sqlalchemy import select

    from app.models.database import ParseRun
    from app.models.execution import ParseFieldJob

    class CorrectingProvider(FakeProvider):
        async def create(self, **kwargs):
            prompt = json.loads(kwargs["messages"][-1]["content"][0]["text"])
            self.calls += 1
            decisions = []
            for proposal in prompt["candidates"]:
                visual = proposal.get("stage") == "original_image_transcription"
                if visual:
                    decisions.append({"candidate_id": proposal["candidate_id"], "judgement": "accepted",
                        "value": "1.42", "unit": "MPa", "value_kind": "measured", "dimensions": {},
                        "source_role": "report_body", "transcription": "报告正文 瓦斯压力 1.42MPa 实测"})
                else:
                    decisions.append({"candidate_id": proposal["candidate_id"], "judgement": "accepted",
                        "value": "1.42" if "1.42" in proposal["text"] else "1.4",
                        "unit": "MPa", "value_kind": "measured", "dimensions": {},
                        "source_role": proposal.get("source_role_hint"), "evidence_ids": proposal["evidence_ids"]})
            return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"decisions": decisions})}}],
                    "usage": {"prompt_tokens": 40, "completion_tokens": 20, "total_tokens": 60}}

    image = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + (1).to_bytes(4, "big") * 2
    async def render(document, pages, candidate):
        return image, {"image_hash": hashlib.sha256(image).hexdigest(), "page_number": 1,
                       "bbox": [0, 0, 500, 40], "source_file_hash": document.file_hash}

    run_id = await make_run(sessions, count=2, source="ocr", text_override="瓦斯压力 1.4ZMPa 实测")
    executor = ReliableFieldExecutor(sessions,
        ModelGateway(CorrectingProvider(), CallLedger(sessions), ServiceCircuit(MemoryCircuitStore())),
        ReliableFieldExecutorConfig("test", "test"), image_provider=render)
    service = ReliableExtractionService()
    result = await service.process_run(sessions, run_id, executor=executor, writer=executor.write_result,
        before_publish=executor.before_publish, owner="correction", max_quantums=50, lease_seconds=300)
    assert result["status"] == "SUPERSEDED"
    assert result["successor_run_id"]
    successor = UUID(result["successor_run_id"])
    result = await service.process_run(sessions, successor, executor=executor, writer=executor.write_result,
        before_publish=executor.before_publish, owner="successor", max_quantums=50, lease_seconds=300)
    assert result["status"] == "COMPLETED"
    async with sessions() as db:
        jobs = (await db.execute(select(ParseFieldJob).where(ParseFieldJob.run_id == successor))).scalars().all()
        assert len(jobs) == 2
        assert all(job.semantic_state == "completed" for job in jobs)
        assert (await db.get(ParseRun, run_id)).status == "SUPERSEDED"
