"""G1 proof that grouped scans reduce work while preserving final-page facts and old checkpoints."""

import json
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models.database import Document, DocumentArtifact, ExtractedFieldModel
from app.models.execution import ParseFieldJob, ParseRangeWorkItem
from app.rules.spec import RuleSpec
from app.services.call_ledger import CallLedger
from app.services.model_gateway import ModelGateway
from app.services.reliable_extraction_service import ReliableExtractionService
from app.services.reliable_field_executor import ReliableFieldExecutor, ReliableFieldExecutorConfig
from app.services.service_circuit import MemoryCircuitStore, ServiceCircuit
from tests.test_field_scheduler_postgres import sessions as pg_sessions
from tests.test_reliable_field_executor import FakeProvider

sessions = pg_sessions


class FinalPageProvider(FakeProvider):
    async def create(self, **kwargs):
        self.calls += 1
        prompt = json.loads(kwargs["messages"][-1]["content"][0]["text"])
        results = []
        for item in prompt["candidates"]:
            decision = {"candidate_id": item["candidate_id"], "judgement": "rejected"}
            if "瓦斯压力" in item["text"]:
                decision.update(judgement="accepted", value="1.23", unit="MPa", value_kind="measured",
                    source_role=item.get("source_role_hint"), dimensions={}, evidence_ids=item["evidence_ids"])
            results.append(decision)
        return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"decisions": results})}}],
                "usage": {"prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70}}


async def start_run(sessions):
    document_id = uuid4()
    pages = []
    for n in range(1, 10):
        text = "报告正文 瓦斯压力 1.23MPa 实测" if n == 9 else "报告正文 普通说明未涉及测定结果"
        pages.append({"page_number": n, "width": 600, "height": 800, "text": text, "native_text": text,
            "image_count": 0, "ocr_status": "not_needed", "tables": [],
            "text_blocks": [{"block_id": f"p{n}", "source": "native", "text": text, "bbox": None}]})
    async with sessions() as db, db.begin():
        db.add(Document(document_id=document_id, file_name="scan-nine.pdf", original_name="synthetic-nine.pdf",
            file_path="test/scan-nine", file_hash="b" * 64, file_size=1, mime_type="application/pdf"))
        await db.flush()
        db.add(DocumentArtifact(document_id=document_id, file_hash="b" * 64, status="READY",
            preprocess_profile_version="scan-quantum-test", pages=pages, quality={"coverage_manifest": {
                "expected_page_count": 9, "pages": [{"page_number": n, "scope_complete": True,
                    "regions": [{"state": "processed"}]} for n in range(1, 10)]}}))
        await db.flush()
        run = await ReliableExtractionService().start(db, document_id, rules=[RuleSpec(
            field_key=f"pressure{i}", field_name_cn="瓦斯压力", data_type="float", unit="MPa", value_kind="measured")
            for i in range(12)])
        return run.parse_run_id


async def execute(sessions, run_id, ranges_per_quantum, *, legacy_scan=False):
    provider = FinalPageProvider()
    executor = ReliableFieldExecutor(sessions,
        ModelGateway(provider, CallLedger(sessions), ServiceCircuit(MemoryCircuitStore())),
        ReliableFieldExecutorConfig("test", "test", ranges_per_quantum=ranges_per_quantum))
    widths = []
    async def quantum(token, checkpoint):
        outcome = await executor(token, checkpoint)
        scans = outcome.checkpoint.get("scan_batches") or []
        if scans:
            widths.append(len(scans))
            if legacy_scan:
                assert len(scans) == 1
                outcome.checkpoint["scan_batch"] = outcome.checkpoint.pop("scan_batches")[0]
        return outcome
    result = await ReliableExtractionService().process_run(sessions, run_id, executor=quantum,
        writer=executor.write_result, before_publish=executor.before_publish,
        owner="grouped-scans", max_quantums=300, lease_seconds=300)
    assert result["status"] == "COMPLETED"
    async with sessions() as db:
        fields = (await db.execute(select(ExtractedFieldModel).where(ExtractedFieldModel.parse_run_id == run_id))).scalars().all()
        assert len(fields) == 12
        assert all({fact["value"] for fact in field.instances} == {"1.23"} for field in fields)
        assert all(any(fact["page_num"] == 9 for fact in field.instances) for field in fields)
        jobs = (await db.execute(select(ParseFieldJob).where(ParseFieldJob.run_id == run_id))).scalars().all()
        work = (await db.execute(select(ParseRangeWorkItem).where(ParseRangeWorkItem.job_id.in_([job.job_id for job in jobs])))).scalars().all()
        assert len(work) == 12 * 12
        assert all(item.scan_done and item.cursor["done"] for item in work)
        assert all("scan_batch" not in job.checkpoint and "scan_batches" not in job.checkpoint for job in jobs)
    return result["quantums_executed"], provider.calls, widths


@pytest.mark.asyncio
async def test_grouped_scans_reduce_nine_page_twelve_field_quantums_with_final_page_facts(sessions):
    baseline_id, grouped_id = await start_run(sessions), await start_run(sessions)
    baseline, baseline_calls, _ = await execute(sessions, baseline_id, 1, legacy_scan=True)
    grouped, grouped_calls, widths = await execute(sessions, grouped_id, 4)
    assert baseline_calls == grouped_calls  # Same model/semantic work and scope denominator.
    assert max(widths) == 4
    assert grouped <= baseline * 0.75
