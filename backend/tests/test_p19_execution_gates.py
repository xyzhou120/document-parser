"""P19 E02/E07/E08 工程检查。

E02 和 E07 是确定性的服务测试。E08 使用授权的 G1 PostgreSQL 夹具，
除非调用方明确选择该数据库，否则跳过。
"""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.models.database import Document, DocumentArtifact, ExtractedFieldModel, ParseRun
from app.models.execution import ParseCandidate, ParseFieldJob, ParseModelCall
from app.rules.spec import RuleSpec
from app.services.call_ledger import CallLedger
from app.services.candidate_retrieval import FieldSpec, retrieve
from app.services.execution import JobRepository, QuantumOutcome
from app.services.model_gateway import CallRequest, CandidateInput, GatewayPolicy, ModelGateway
from app.services.reliable_extraction_service import ReliableExtractionService
from app.services.reliable_field_executor import ReliableFieldExecutor, ReliableFieldExecutorConfig
from app.services.service_circuit import CircuitPolicy, MemoryCircuitStore, ServiceCircuit
from tests.test_field_scheduler_postgres import seed
from tests.test_field_scheduler_postgres import sessions as pg_sessions
from tests.test_reliable_field_executor import make_run

sessions = pg_sessions

def pressure_spec():
    return FieldSpec("pressure", "瓦斯压力", ["瓦斯压力"], "MPa", True, True)


def test_e02_hundreds_of_candidates_keep_last_page_match_without_top_n_loss():
    pages = [{"page_number": page, "text_blocks": [{
        "block_id": f"b{page}", "source": "native",
        "text": f"报告正文 瓦斯压力 {page}.10 MPa 实测",
        "bbox": [0, 0, 300, 20],
    }], "text": "", "image_count": 0, "ocr_status": "not_needed"} for page in range(1, 251)]
    result = retrieve(pressure_spec(), pages, max_candidates=None, content_revision="rev", rule_semantic_hash="rule")
    assert result.total_matches == 250
    assert len(result.candidates) == 250
    assert result.candidates[-1].page_number == 250
    assert result.candidates[-1].numbers == ["250.10"]
    assert len({candidate.stable_key for candidate in result.candidates}) == 250


@pytest.mark.asyncio
async def test_e07_one_hundred_fields_share_a_circuit_and_resume_after_probe():
    now = [0.0]
    class Clock:
        def __call__(self):
            return datetime.fromtimestamp(now[0])

    circuit = ServiceCircuit(MemoryCircuitStore(), CircuitPolicy(threshold=3, cooldown_seconds=10,
                                                                 half_open_limit=1, max_recoveries=3), clock=Clock())
    key = "model:shared-provider"
    permits = []
    for _ in range(3):
        permit = await circuit.acquire(key)
        assert permit.allowed
        permits.append(permit)
        await circuit.failure(key, permit)
    blocked = [await circuit.acquire(key) for _ in range(100)]
    assert all(not permit.allowed for permit in blocked)
    now[0] = 11.0
    probe = await circuit.acquire(key)
    assert probe.allowed and probe.probe
    await circuit.success(key, probe)
    recovered = [await circuit.acquire(key) for _ in range(100)]
    assert all(permit.allowed for permit in recovered)
    for permit in recovered:
        await circuit.release(key, permit)


@pytest.mark.asyncio
async def test_e08_new_rule_child_run_freezes_independent_denominator_and_parent_snapshot(sessions):
    parent_rule = RuleSpec(field_key="pressure", field_name_cn="瓦斯压力", data_type="float", unit="MPa",
                           value_kind="measured", strategy={"semantic_contract": {"cardinality": "single"}})
    child_rule = RuleSpec(field_key="holes", field_name_cn="钻孔数", data_type="integer", unit="个",
                          value_kind="actual", strategy={"semantic_contract": {"cardinality": "single"}})
    parent_id = await make_run(sessions, rules_override=[parent_rule])
    async with sessions() as db, db.begin():
        parent = await db.get(ParseRun, parent_id)
        original_rules = deepcopy(parent.extraction_profile["rules"])
        child = await ReliableExtractionService().start(
            db, parent.document_id, rules=[child_rule], parent_run_id=parent_id, request_id=f"child-{uuid4()}"
        )
        assert parent.requested_field_keys == ["pressure"]
        assert parent.extraction_profile["rules"] == original_rules
        assert child.parent_parse_run_id == parent_id
        assert child.requested_field_keys == ["holes"]
        assert child.extraction_profile["rules"] != parent.extraction_profile["rules"]
        child_id = child.parse_run_id
    # 创建后修改来源规则不能改变任何一个已持久化快照。
    parent_rule.field_name_cn = "运行后修改"
    child_rule.field_name_cn = "运行后新增规则又改名"
    async with sessions() as db:
        parent, child = await db.get(ParseRun, parent_id), await db.get(ParseRun, child_id)
        parent_jobs = (await db.execute(select(ParseFieldJob).where(ParseFieldJob.run_id == parent_id))).scalars().all()
        child_jobs = (await db.execute(select(ParseFieldJob).where(ParseFieldJob.run_id == child_id))).scalars().all()
        assert [job.field_key for job in parent_jobs] == ["pressure"]
        assert [job.field_key for job in child_jobs] == ["holes"]
        assert parent.extraction_profile["rules"] == original_rules
        assert parent_jobs[0].checkpoint["rule_snapshot"]["field_name_cn"] == "瓦斯压力"
        assert child_jobs[0].checkpoint["rule_snapshot"]["field_name_cn"] == "钻孔数"
        assert child.content_revision_id == parent.content_revision_id


class RejectingProvider:
    """模拟服务响应，用于测试传输和恢复，不用于测试模型质量。"""

    def __init__(self, failures=0):
        self.calls = 0
        self.failures = failures
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self, **kwargs):
        import json
        self.calls += 1
        if self.calls <= self.failures:
            raise TimeoutError("synthetic shared provider outage")
        payload = json.loads(kwargs["messages"][-1]["content"][0]["text"])
        decisions = [{"candidate_id": item["candidate_id"], "judgement": "rejected"}
                     for item in payload["candidates"]]
        return {"choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps({"decisions": decisions})}}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}}


@pytest.mark.asyncio
async def test_e07_pg_one_hundred_fields_wait_once_then_automatically_resume(sessions):
    run_id, _, job_ids = await seed(sessions, 100)
    clock_value = [datetime.now(UTC).replace(tzinfo=None)]
    provider = RejectingProvider(failures=3)
    circuit = ServiceCircuit(MemoryCircuitStore(), CircuitPolicy(threshold=3, cooldown_seconds=30,
        half_open_limit=1, max_recoveries=8), clock=lambda: clock_value[0])
    gateway = ModelGateway(provider, CallLedger(sessions), circuit, GatewayPolicy(backoff_seconds=1),
        clock=lambda: clock_value[0])

    async def execute(token, checkpoint):
        prior = checkpoint.get("prior_call_id")
        outcome = await gateway.execute(CallRequest(job_id=token.job_id, fence=token.fence_token,
            provider_key="p19:100-shared-service", dispatch_key=f"p19:{token.job_id}:{token.fence_token}",
            checkpoint_key="p19:single-check", candidates=(CandidateInput("c", {"text": "synthetic no match"}),),
            model="synthetic", estimated_input_tokens=0,
            retry_seq=checkpoint.get("retry_seq", 0), retry_of_call_id=UUID(prior) if prior else None))
        if outcome.wait_state:
            cp = {**checkpoint, "prior_call_id": outcome.call_id,
                  "retry_seq": outcome.retry_seq or checkpoint.get("retry_seq", 0), "wait_seen": True}
            return QuantumOutcome(outcome.wait_state, cp, retry_at=datetime.fromisoformat(outcome.retry_at), error=outcome.error)
        assert outcome.decisions == [{"candidate_id": "c", "judgement": "rejected"}]
        return QuantumOutcome("done", {**checkpoint, "recovered": True}, semantic_state="not_found")

    service = ReliableExtractionService()
    waiting = await service.process_run(sessions, run_id, executor=execute, owner="p19-outage", max_quantums=100)
    assert waiting["status"] == "WAITING_SERVICE"
    assert provider.calls == 3  # 共享故障不会产生 100 次超时。
    async with sessions() as db:
        jobs = (await db.execute(select(ParseFieldJob).where(ParseFieldJob.run_id == run_id))).scalars().all()
        assert len(jobs) == 100 and all(job.execution_state == "waiting_service" for job in jobs)
        assert all(job.checkpoint.get("wait_seen") for job in jobs)
    clock_value[0] += timedelta(seconds=31)
    async with sessions() as db, db.begin():
        assert await JobRepository(db).requeue_due(now=clock_value[0], limit=500) >= 100
    recovered = await service.process_run(sessions, run_id, executor=execute, owner="p19-recovery", max_quantums=100)
    assert recovered["status"] == "COMPLETED"
    assert provider.calls == 103
    async with sessions() as db:
        jobs = (await db.execute(select(ParseFieldJob).where(ParseFieldJob.run_id == run_id))).scalars().all()
        assert all(job.execution_state == "done" and job.checkpoint["recovered"] for job in jobs)
        calls = (await db.execute(select(ParseModelCall).where(ParseModelCall.job_id.in_(job_ids)))).scalars().all()
        assert len([call for call in calls if call.retry_of_call_id]) == 100


@pytest.mark.asyncio
async def test_e02_pg_hundreds_of_candidates_reach_unlabelled_last_page_target(sessions):
    """真实 P06/P07/P10 写入方和 PG 游标；提供方相关性仍由模拟数据表示。"""
    import json

    class LastPageProvider(RejectingProvider):
        async def create(self, **kwargs):
            response = await super().create(**kwargs)
            payload = json.loads(kwargs["messages"][-1]["content"][0]["text"])
            output = json.loads(response["choices"][0]["message"]["content"])
            for candidate, decision in zip(payload["candidates"], output["decisions"], strict=True):
                if "P19_FINAL_TARGET" in candidate["text"]:
                    decision.update(judgement="accepted", value="0.20", unit="MPa", value_kind="measured",
                        source_role=candidate["source_role_hint"], evidence_ids=candidate["evidence_ids"], dimensions={})
            response["choices"][0]["message"]["content"] = json.dumps(output)
            return response

    document_id = uuid4()
    pages = []
    ordinal = 0
    for number, count in enumerate((70, 70, 59), start=1):
        blocks = []
        for line in range(count):
            ordinal += 1
            blocks.append({"text": f"报告正文 历史非目标记录 瓦斯压力 {ordinal}.10MPa 实测",
                "source": "native", "block_id": f"p19-{number}-{line}",
                "bbox": [0, line * 20, 500, line * 20 + 10]})
        if number == 3:
            # 没有配置标签时，只有完整范围回退能找到此值。
            blocks.append({"text": "报告正文 P19_FINAL_TARGET 最新测点 实测0.20MPa",
                "source": "native", "block_id": "p19-final", "bbox": [0, 1300, 500, 1310]})
        pages.append({"page_number": number, "width": 600, "height": 2000,
            "text": "\n".join(block["text"] for block in blocks), "text_blocks": blocks,
            "image_count": 0, "ocr_status": "not_needed", "tables": []})
    rule = RuleSpec(field_key="final_pressure", field_name_cn="瓦斯压力", data_type="float", unit="MPa",
        value_kind="measured", field_description="仅提取最新测点，历史非目标记录排除")
    async with sessions() as db, db.begin():
        db.add(Document(document_id=document_id, file_name="p19-many.pdf", original_name="synthetic-p19-many.pdf",
            file_path="synthetic/p19-many", file_size=1, file_hash="a" * 64, mime_type="application/pdf"))
        await db.flush()
        db.add(DocumentArtifact(document_id=document_id, file_hash="a" * 64, status="READY",
            preprocess_profile_version="p19-test", pages=pages, quality={"coverage_manifest": {
                "expected_page_count": 3, "pages": [{"page_number": n, "scope_complete": True,
                    "regions": [{"state": "processed", "whole_page": True}]} for n in (1, 2, 3)]}}))
        await db.flush()
        run = await ReliableExtractionService().start(db, document_id, rules=[rule])
        run_id = run.parse_run_id
    provider = LastPageProvider()
    executor = ReliableFieldExecutor(sessions,
        ModelGateway(provider, CallLedger(sessions), ServiceCircuit(MemoryCircuitStore())),
        ReliableFieldExecutorConfig("p19-last-page", "synthetic", candidates_per_call=12))
    service = ReliableExtractionService()
    windows = 0
    while windows < 30:
        result = await service.process_run(sessions, run_id, executor=executor, writer=executor.write_result,
            before_publish=executor.before_publish, owner="p19-many", max_quantums=7, lease_seconds=300)
        windows += 1
        if result["status"] in {"COMPLETED", "FAILED", "COMPLETED_WITH_REVIEW"}:
            break
    assert result["status"] == "COMPLETED"
    assert windows > 1  # 多个内部量子自动继续，无需手动恢复。
    async with sessions() as db:
        job = await db.scalar(select(ParseFieldJob).where(ParseFieldJob.run_id == run_id))
        candidates = (await db.execute(select(ParseCandidate).where(ParseCandidate.job_id == job.job_id))).scalars().all()
        field = await db.scalar(select(ExtractedFieldModel).where(ExtractedFieldModel.parse_run_id == run_id))
        assert len(candidates) >= 200
        assert all(candidate.state in {"accepted", "rejected", "duplicate"} for candidate in candidates)
        assert any(candidate.state == "accepted" and candidate.block_ref["page"] == 3 for candidate in candidates)
        assert {fact["value"] for fact in field.instances} == {"0.20"}
        assert field.extraction_detail["field_completion"]["coverage_complete"] is True
