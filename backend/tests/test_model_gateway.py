import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.services.call_ledger import (
    AdminHardLimitExceeded,
    CallLedger,
    MemoryCallLedger,
    accounted_tokens,
    new_call,
)
from app.services.model_gateway import (
    CallRequest,
    CandidateInput,
    GatewayPolicy,
    ModelGateway,
    build_messages,
    parse_response_json,
    split_candidates,
)
from app.services.service_circuit import (
    CircuitPolicy,
    MemoryCircuitStore,
    ServiceCircuit,
    service_key,
)
from tests.test_field_scheduler_postgres import seed as seed_pg
from tests.test_field_scheduler_postgres import sessions as pg_sessions

sessions = pg_sessions


class FakeClient:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []
        self.chat = self.completions = self

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


class HTTPError(Exception):
    def __init__(self, status, code=None):
        self.status_code, self.body = status, {"error": {"code": code}}


def response(decisions=(), *, finish="stop", usage=True, content=None):
    return SimpleNamespace(id="provider-request", choices=[SimpleNamespace(
        message=SimpleNamespace(content=content if content is not None else
                                json.dumps({"decisions": list(decisions)})),
        finish_reason=finish)],
        usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20, total_tokens=120,
                              completion_tokens_details=SimpleNamespace(reasoning_tokens=5))
        if usage else None)


def decision(key, judgement="rejected"):
    return {"candidate_id": key, "judgement": judgement}


def request(*keys, **kwargs):
    return CallRequest(job_id=uuid4(), fence=1, provider_key="model:test:v1",
                       dispatch_key="dispatch-1", checkpoint_key="batch-1",
                       candidates=tuple(CandidateInput(key, {"text": "value 1"}) for key in keys),
                       model="test", estimated_input_tokens=500, **kwargs)


def gateway(responses, *, policy=None, circuit_policy=None, validator=None):
    client, ledger = FakeClient(responses), MemoryCallLedger(unknown_seconds=30)
    circuit = ServiceCircuit(MemoryCircuitStore(), circuit_policy)
    return ModelGateway(client, ledger, circuit, policy, decision_validator=validator), client, ledger


def test_length_discards_all_decisions_and_bisects_with_original_images():
    async def run():
        gw, client, ledger = gateway([response([decision("a")], finish="length")])
        req = request("a", "b")
        req = replace(req, candidates=tuple(replace(c, source="ocr", image_refs=("image-a",))
                                             for c in req.candidates))
        result = await gw.execute(req)
        assert result.state == "length" and result.decisions == []
        assert result.pending_ids == ["a", "b"] and result.batch_state == "split"
        assert [b["candidate_ids"] for b in result.split_batches] == [["a"], ["b"]]
        assert all(b["candidates"][0]["image_refs"] == ("image-a",) for b in result.split_batches)
        call = await ledger.get(UUID(result.call_id))
        assert call.state == "length" and call.usage["total_tokens"] == 120
        assert call.usage["reasoning_tokens"] == 5
        assert client.calls[0]["messages"][-1]["content"][-1]["image_url"]["url"] == "image-a"
    asyncio.run(run())


@pytest.mark.parametrize("content", ['{"decisions":[', '[]', '{"decisions":{}}'])
def test_invalid_json_splits_whole_batch(content):
    async def run():
        gw, _, _ = gateway([response(content=content)])
        result = await gw.execute(request("a", "b"))
        assert result.state == "invalid" and not result.decisions
        assert len(result.split_batches) == 2
    asyncio.run(run())


@pytest.mark.parametrize("content", [
    '```json\n{"decisions":[{"candidate_id":"a","judgement":"rejected"}]}\n```',
    'Here is the JSON:\n{"decisions":[{"candidate_id":"a","judgement":"rejected"}]}',
])
def test_json_decoder_accepts_single_fence_or_safe_prefix_without_repairing_fields(content):
    assert parse_response_json(content)["decisions"][0]["candidate_id"] == "a"


@pytest.mark.parametrize("content", [
    '```json\n{"decisions":[]}\n```\n```json\n{}\n```',
    '{"decisions":[]} trailing {"another":1}',
    '{"decisions":[{"candidate_id":"a","candidate_id":"b"}]}',
])
def test_json_decoder_rejects_ambiguous_or_duplicate_json(content):
    with pytest.raises(ValueError):
        parse_response_json(content)


def test_invalid_outcome_contains_bounded_digest_excerpt_without_secret_or_images():
    async def run():
        secret = 'Bearer sk-secret data:image/png;base64,AAAA'
        gw, _, ledger = gateway([response(content=f"```json\n{secret}\n```")])
        outcome = await gw.execute(request("a", "b"))
        assert outcome.state == "invalid" and outcome.diagnostics["response_sha256"]
        assert "sk-secret" not in outcome.diagnostics["response_excerpt"]
        assert "data:image" not in outcome.diagnostics["response_excerpt"]
        call = await ledger.get(UUID(outcome.call_id))
        stored = call.reservation["gateway_outcome"]
        assert stored["diagnostics"]["response_sha256"] == outcome.diagnostics["response_sha256"]
    asyncio.run(run())


def test_prompt_deduplicates_shared_rule_and_drops_registry_and_image_base64_payload():
    rule = {"field_key": "pressure", "instructions": "同一规则"}
    req = request("a", "b")
    candidates = tuple(replace(item, payload={"text": "x", "rule": rule,
                                                "evidence_registry": {"secret": "bad"},
                                                "image_base64": "data:image/png;base64,AAAA"},
                              image_refs=("data:image/png;base64,AAAA",)) for item in req.candidates)
    message = build_messages(replace(req, candidates=candidates))[-1]["content"][0]["text"]
    assert message.count('"field_rule"') == 1 and 'evidence_registry' not in message
    assert 'image_base64' not in message and 'AAAA' not in message


def test_valid_envelope_only_commits_unique_complete_known_candidate_items():
    async def run():
        gw, _, _ = gateway([response([decision("a"), decision("b"), decision("b"),
                                     decision("invented"), decision("c", "unfinished")])])
        result = await gw.execute(request("a", "b", "c", "d"))
        assert result.decisions == [decision("a")]
        assert result.pending_ids == ["b", "c", "d"]
        assert result.batch_state == "partially_committed"
    asyncio.run(run())


def test_semantic_validator_rejects_unsupported_candidate_without_losing_other_items():
    async def run():
        gw, _, _ = gateway([response([decision("a"), decision("b")])],
                           validator=lambda item, candidate: candidate.candidate_id == "b")
        result = await gw.execute(request("a", "b"))
        assert result.decisions == [decision("b")] and result.pending_ids == ["a"]
    asyncio.run(run())


def test_duplicate_dispatch_replays_record_without_sending_or_charging_twice():
    async def run():
        gw, client, ledger = gateway([response([decision("a")])])
        req = request("a")
        first, second = await gw.execute(req), await gw.execute(req)
        assert first.call_id == second.call_id and second.replayed
        assert second.decisions == first.decisions and len(client.calls) == 1
        assert (await ledger.usage_totals(req.job_id))["calls"] == 1
        with pytest.raises(ValueError, match="different"):
            await gw.execute(replace(req, candidates=(CandidateInput("b", {}),)))
    asyncio.run(run())


def test_concurrent_duplicate_dispatch_only_sends_once():
    async def run():
        gw, client, _ = gateway([response([decision("a")])])
        req = request("a")
        results = await asyncio.gather(gw.execute(req), gw.execute(req))
        assert len(client.calls) == 1
        assert {r.call_id for r in results} == {results[0].call_id}
    asyncio.run(run())


def test_unknown_usage_preserves_reservation_and_reconciles_idempotently():
    async def run():
        gw, _, ledger = gateway([response([decision("a")], usage=False)])
        req = request("a")
        result = await gw.execute(req)
        call_id = UUID(result.call_id)
        before = await ledger.get(call_id)
        assert before.charge_state == "uncertain" and before.usage is None
        assert accounted_tokens(before) > 0
        assert await ledger.expire_unknown(now=before.unknown_deadline + timedelta(seconds=1))
        expired = await ledger.get(call_id)
        assert accounted_tokens(expired) == accounted_tokens(before)
        assert expired.usage_quality == "estimated" and expired.unknown_deadline is None
        assert await ledger.reconcile(call_id, {"total_tokens": 321}, 3)
        assert not await ledger.reconcile(call_id, {"total_tokens": 999}, 3)
        assert (await ledger.get(call_id)).usage == {"total_tokens": 321}
    asyncio.run(run())


def test_timeout_retry_has_new_call_id_and_independent_charge():
    async def run():
        gw, client, ledger = gateway([TimeoutError(), response([decision("a")])])
        req = request("a")
        first = await gw.execute(req)
        assert first.state == "timeout" and first.retry_seq == 1 and first.retry_at
        second = await gw.execute(replace(req, dispatch_key="dispatch-2", retry_seq=1,
                                          retry_of_call_id=UUID(first.call_id)))
        assert second.state == "succeeded" and second.call_id != first.call_id
        totals = await ledger.usage_totals(req.job_id)
        assert totals["calls"] == len(client.calls) == 2 and totals["uncertain_calls"] == 1
    asyncio.run(run())


@pytest.mark.parametrize("status,code,wait,error", [
    (429, "rate_limit_exceeded", "waiting_service", "transient"),
    (503, None, "waiting_service", "transient"),
    (429, "insufficient_quota", "waiting_external_quota", "external_quota"),
    (401, None, None, "configuration"),
])
def test_http_errors_distinguish_transient_quota_and_config(status, code, wait, error):
    async def run():
        gw, _, _ = gateway([HTTPError(status, code)])
        result = await gw.execute(request("a"))
        assert result.wait_state == wait and result.error == error
    asyncio.run(run())


def test_retry_limit_is_bounded_and_never_sends_excess_attempt():
    async def run():
        gw, client, _ = gateway([TimeoutError()], policy=GatewayPolicy(retry_max=0))
        req = request("a")
        result = await gw.execute(req)
        assert result.error == "retry_exhausted:timeout" and result.retry_at is None
        result = await gw.execute(replace(req, retry_seq=1, retry_of_call_id=UUID(result.call_id)))
        assert result.state == "retry_exhausted" and len(client.calls) == 1
    asyncio.run(run())


def test_context_or_image_capacity_returns_split_without_paid_call():
    async def run():
        gw, client, ledger = gateway([], policy=GatewayPolicy(context_capacity=4000))
        req = replace(request("a", "b"), estimated_input_tokens=2000)
        result = await gw.execute(req)
        assert result.state == "context_overflow" and len(result.split_batches) == 2
        assert client.calls == [] and (await ledger.usage_totals(req.job_id))["calls"] == 0
    asyncio.run(run())


def test_single_candidate_splits_independent_units_with_context_and_image():
    req = request("a")
    candidate = CandidateInput("a", {"context": "table header"}, ("original-image",), "ocr",
                               ({"object": "one"}, {"object": "two"}, {"object": "three"}))
    req = replace(req, candidates=(candidate,))
    children = split_candidates(req)
    assert [len(child["candidates"][0]["units"]) for child in children] == [1, 2]
    assert all(child["candidates"][0]["payload"]["context"] == "table header" for child in children)
    assert all(child["candidates"][0]["image_refs"] == ("original-image",) for child in children)
    assert split_candidates(replace(req, split_path=("0",)), max_depth=1) == []


def test_minimum_unit_length_stays_unresolved_without_infinite_identical_split():
    async def run():
        gw, _, _ = gateway([response(finish="length")])
        result = await gw.execute(request("a"))
        assert result.pending_ids == ["a"] and result.split_batches == []
        assert result.error.startswith("minimum_unit_unresolved")
    asyncio.run(run())


def test_ocr_input_requires_image_and_fake_payload_cannot_override_candidate_id():
    with pytest.raises(ValueError, match="original image"):
        CandidateInput("a", {}, source="ocr")
    async def run():
        gw, client, _ = gateway([response([decision("a")])])
        req = replace(request("a"), candidates=(CandidateInput("a", {"candidate_id": "invented"}),))
        await gw.execute(req)
        prompt = json.loads(client.calls[0]["messages"][-1]["content"][0]["text"])
        assert prompt["candidates"][0]["candidate_id"] == "a"
    asyncio.run(run())


def test_crashed_started_call_becomes_uncertain_then_requires_new_dispatch():
    async def run():
        ledger = MemoryCallLedger(unknown_seconds=1)
        now = datetime.utcnow()
        row = new_call(job_id=uuid4(), fence=1, provider_key="m", dispatch_key="d", checkpoint_key="c",
                       candidate_ids=["a"], reservation={"total_tokens": 900}, now=now)
        await ledger.reserve(row)
        assert await ledger.claim(row.call_id, now=now)
        await ledger.started(row.call_id)
        assert not await ledger.claim(row.call_id)
        expired = await ledger.expire_unknown(now=now + timedelta(seconds=2))
        assert expired[0].state == "uncertain" and accounted_tokens(expired[0]) == 900
    asyncio.run(run())


def test_shared_circuit_bounds_100_failed_fields_and_recovers_single_probe():
    async def run():
        now = [datetime(2026, 10, 12)]
        circuit = ServiceCircuit(MemoryCircuitStore(), CircuitPolicy(threshold=2, cooldown_seconds=5),
                                 clock=lambda: now[0])
        client = FakeClient([TimeoutError(), TimeoutError(), response([decision("a")])])
        gw = ModelGateway(client, MemoryCallLedger(), circuit, clock=lambda: now[0])
        for index in range(100):
            result = await gw.execute(replace(request("a"), dispatch_key=f"d-{index}"))
        assert len(client.calls) == 2 and result.state == "waiting_service"
        now[0] += timedelta(seconds=6)
        probes = await asyncio.gather(*[circuit.acquire("model:test:v1") for _ in range(20)])
        assert sum(p.allowed for p in probes) == 1
        probe = next(p for p in probes if p.allowed)
        await circuit.success("model:test:v1", probe)
        assert (await circuit.acquire("model:test:v1")).allowed
        result = await gw.execute(replace(request("a"), dispatch_key="recovered"))
        assert result.state == "succeeded" and len(client.calls) == 3
    asyncio.run(run())


def test_ocr_and_model_circuits_are_independent_and_stale_probe_cannot_close():
    async def run():
        now = [datetime(2026, 10, 12)]
        circuit = ServiceCircuit(MemoryCircuitStore(), CircuitPolicy(threshold=1, cooldown_seconds=5),
                                 clock=lambda: now[0])
        ocr = service_key("ocr", "http://host", "same", "v1")
        model = service_key("model", "http://host", "same", "v1")
        old = await circuit.acquire(ocr)
        await circuit.failure(ocr, old)
        await circuit.success(ocr, old)
        assert not (await circuit.acquire(ocr)).allowed
        assert (await circuit.acquire(model)).allowed
        now[0] += timedelta(seconds=6)
        probe = await circuit.acquire(ocr)
        await circuit.failure(ocr, probe)
        await circuit.success(ocr, probe)
        assert not (await circuit.acquire(ocr)).allowed
    asyncio.run(run())


def test_circuit_recovery_has_bounded_terminal_exhaustion():
    async def run():
        circuit = ServiceCircuit(MemoryCircuitStore(), CircuitPolicy(threshold=1, max_recoveries=1))
        permit = await circuit.acquire("model:test")
        await circuit.failure("model:test", permit)
        denied = await circuit.acquire("model:test")
        assert not denied.allowed and denied.exhausted
    asyncio.run(run())


def test_gateway_circuit_exhausted_has_no_automatic_wait_or_retry():
    async def run():
        gw, client, _ = gateway([TimeoutError()], circuit_policy=CircuitPolicy(threshold=1, max_recoveries=1))
        await gw.execute(request("a"))
        outcome = await gw.execute(replace(request("a"), dispatch_key="new-field"))
        assert outcome.state == "service_exhausted"
        assert outcome.wait_state is None and outcome.retry_at is None
        assert outcome.error == "service_recovery_exhausted" and len(client.calls) == 1
    asyncio.run(run())


def test_external_quota_recovery_uses_new_dispatch_and_retains_old_charge():
    async def run():
        gw, client, ledger = gateway([HTTPError(429, "insufficient_quota"), response([decision("a")])])
        req = request("a")
        first = await gw.execute(req)
        assert first.wait_state == "waiting_external_quota"
        with pytest.raises(ValueError, match="new dispatch"):
            await gw.requeue_external_quota(req, prior_call_id=first.call_id, dispatch_key=req.dispatch_key)
        second = await gw.requeue_external_quota(req, prior_call_id=first.call_id, dispatch_key="quota-restored-1")
        replay = await gw.requeue_external_quota(req, prior_call_id=first.call_id, dispatch_key="quota-restored-1")
        assert second.state == "succeeded" and replay.replayed and second.call_id != first.call_id
        assert len(client.calls) == 2
        old, new = await ledger.get(UUID(first.call_id)), await ledger.get(UUID(second.call_id))
        assert old.charge_state == "uncertain" and new.retry_of_call_id == old.call_id
        assert new.retry_seq == old.retry_seq + 1
        assert (await ledger.usage_totals(req.job_id))["calls"] == 2
    asyncio.run(run())


def test_unset_hard_limit_does_not_stop_normal_calls():
    async def run():
        gw, client, _ = gateway([response([decision("a")]), response([decision("a")])])
        first = await gw.execute(request("a"))
        second = await gw.execute(replace(request("a"), dispatch_key="d2"))
        assert first.state == second.state == "succeeded" and len(client.calls) == 2
    asyncio.run(run())


def test_explicit_admin_hard_limit_blocks_before_model_without_silent_not_found():
    async def run():
        gw, client, ledger = gateway([response([decision("a")]), response([decision("a")])])
        # Reserve 120 actual tokens on the first call; second reservation is 6500,
        # so a 6000-token explicit ceiling must wait before transport.
        first = await gw.execute(replace(request("a"), hard_limit_tokens=7000))
        second = await gw.execute(replace(request("a"), dispatch_key="d2", hard_limit_tokens=6000))
        assert first.state == "succeeded"
        assert second.state == "waiting_external_quota" and second.wait_state == "waiting_external_quota"
        assert second.error == "admin_hard_limit" and len(client.calls) == 1
        blocked = await ledger.get(UUID(second.call_id))
        assert blocked.charge_state == "reserved" and accounted_tokens(blocked) == 0
    asyncio.run(run())


def test_hard_limit_concurrent_claims_allow_only_fitting_reservation():
    async def run():
        ledger = MemoryCallLedger()
        first = new_call(job_id=uuid4(), fence=1, provider_key="m", dispatch_key="a", checkpoint_key="c",
                         candidate_ids=["a"], reservation={"total_tokens": 600})
        second = new_call(job_id=uuid4(), fence=1, provider_key="m", dispatch_key="b", checkpoint_key="c",
                          candidate_ids=["b"], reservation={"total_tokens": 600})
        run_id = uuid4()
        ledger.register_job(first.job_id, run_id)
        ledger.register_job(second.job_id, run_id)
        await ledger.reserve(first)
        await ledger.reserve(second)
        results = await asyncio.gather(
            ledger.claim(first.call_id, hard_limit_tokens=1000),
            ledger.claim(second.call_id, hard_limit_tokens=1000),
            return_exceptions=True)
        assert sum(result is True for result in results) == 1
        assert sum(isinstance(result, AdminHardLimitExceeded) for result in results) == 1
    asyncio.run(run())


def test_actual_reconciliation_releases_reservation_but_unknown_keeps_upper_bound():
    async def run():
        ledger = MemoryCallLedger(unknown_seconds=1)
        job = uuid4()
        first = new_call(job_id=job, fence=1, provider_key="m", dispatch_key="a", checkpoint_key="c",
                         candidate_ids=["a"], reservation={"total_tokens": 600})
        second = new_call(job_id=job, fence=1, provider_key="m", dispatch_key="b", checkpoint_key="c",
                          candidate_ids=["b"], reservation={"total_tokens": 600})
        await ledger.reserve(first)
        await ledger.reserve(second)
        assert await ledger.claim(first.call_id, hard_limit_tokens=1000)
        await ledger.finish(first.call_id, state="timeout", now=datetime(2026, 10, 12))
        await ledger.expire_unknown(now=datetime(2026, 10, 13))
        assert accounted_tokens(await ledger.get(first.call_id)) == 600
        with pytest.raises(AdminHardLimitExceeded):
            await ledger.claim(second.call_id, hard_limit_tokens=1000)
        assert await ledger.reconcile(first.call_id, {"total_tokens": 100}, 2)
        assert accounted_tokens(await ledger.get(first.call_id)) == 100
        assert await ledger.claim(second.call_id, hard_limit_tokens=1000)
    asyncio.run(run())


def test_recursive_single_candidate_unit_work_ids_do_not_change_after_split():
    original = CandidateInput("a", {}, units=tuple({"object": str(index)} for index in range(5)))
    req = replace(request("a"), candidates=(original,))
    children = split_candidates(req)
    assert not set(children[0]["unit_work_ids"]) & set(children[1]["unit_work_ids"])
    child = children[1]
    value = child["candidates"][0]
    nested = split_candidates(replace(req, candidates=(CandidateInput(**value),), split_path=tuple(child["split_path"])))
    assert nested[0]["parent_unit_work_ids"] == child["unit_work_ids"]
    assert set(nested[0]["unit_work_ids"]) | set(nested[1]["unit_work_ids"]) == set(child["unit_work_ids"])


@pytest.mark.asyncio
async def test_postgres_hard_limit_serializes_cross_field_claims_and_late_usage(sessions):
    from app.models.database import ParseRun

    run_id, _, jobs = await seed_pg(sessions, 2)
    async with sessions() as db, db.begin():
        run = await db.get(ParseRun, run_id, with_for_update=True)
        run.extraction_profile = {"hard_limit_tokens": 1000}
    ledger = CallLedger(sessions, unknown_seconds=1)
    records = [new_call(job_id=job_id, fence=1, provider_key="m", dispatch_key=f"hard-{uuid4()}",
                        checkpoint_key="c", candidate_ids=[str(index)], reservation={"total_tokens": 600})
               for index, job_id in enumerate(jobs)]
    for record in records:
        await ledger.reserve(record)
    # The frozen profile wins over a caller trying to raise its local ceiling.
    claims = await asyncio.gather(*(ledger.claim(record.call_id, hard_limit_tokens=9999)
                                    for record in records), return_exceptions=True)
    assert sum(result is True for result in claims) == 1
    assert sum(isinstance(result, AdminHardLimitExceeded) for result in claims) == 1
    sent = records[claims.index(True)]
    blocked = records[1 - claims.index(True)]
    await ledger.finish(sent.call_id, state="timeout", now=datetime(2026, 10, 12))
    await ledger.expire_unknown(now=datetime(2026, 10, 13))
    with pytest.raises(AdminHardLimitExceeded):
        await ledger.claim(blocked.call_id)
    assert await ledger.reconcile(sent.call_id, {"total_tokens": 100}, 2)
    assert await ledger.claim(blocked.call_id)


@pytest.mark.asyncio
async def test_postgres_frozen_none_limit_keeps_long_work_unlimited(sessions):
    from app.models.database import ParseRun

    run_id, _, jobs = await seed_pg(sessions, 2)
    async with sessions() as db, db.begin():
        run = await db.get(ParseRun, run_id, with_for_update=True)
        run.extraction_profile = {"hard_limit_tokens": None}
    ledger = CallLedger(sessions)
    for job_id in jobs:
        record = new_call(job_id=job_id, fence=1, provider_key="m", dispatch_key=f"unlimited-{uuid4()}",
                          checkpoint_key="c", candidate_ids=["a"], reservation={"total_tokens": 10000})
        await ledger.reserve(record)
        assert await ledger.claim(record.call_id, hard_limit_tokens=1)


def test_hard_limit_expansion_uses_new_attempt_and_done_call_is_not_resent():
    async def run():
        gw, client, ledger = gateway([response([decision("a")]), response([decision("a")])])
        req = request("a")
        ledger.register_job(req.job_id, req.job_id)
        done = await gw.execute(req)
        blocked = await gw.execute(replace(req, dispatch_key="blocked", hard_limit_tokens=1))
        assert done.state == "succeeded" and blocked.state == "waiting_external_quota"
        resumed = await gw.requeue_external_quota(replace(req, hard_limit_tokens=10000),
                                                   prior_call_id=blocked.call_id, dispatch_key="expanded")
        assert resumed.state == "succeeded" and resumed.call_id != done.call_id and len(client.calls) == 2
        assert (await ledger.usage_totals(req.job_id))["calls"] == 3
    asyncio.run(run())
