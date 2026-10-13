"""Frozen-content field quanta using P06 retrieval, P07 calls and P10 closure.

Provider requests run after the read session closes.  A guarded writer saves
scan progress, validated decisions and result rows in the runner transaction.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections import OrderedDict
from collections.abc import Callable
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select

from app.models.database import Document, ExtractedFieldModel, ParseRun
from app.models.execution import ParseCandidate, ParseFieldJob
from app.rules.spec import RuleSpec
from app.services.candidate_repository import CandidateRepository
from app.services.candidate_retrieval import Candidate, FieldSpec, normalize
from app.services.content_publish_service import content_publish_service
from app.services.content_service import ContentService
from app.services.evidence_verification import validate_candidate_decision
from app.services.execution import LeaseToken, QuantumOutcome
from app.services.field_completion import complete_field
from app.services.model_gateway import CallRequest, CandidateInput, ModelGateway
from app.services.retrieval_plan import (
    CandidateBatchTree,
    RangeShard,
    ScanCursor,
    plan_rule_ranges,
    scan_candidates,
)


@dataclass(frozen=True)
class ReliableFieldExecutorConfig:
    provider_key: str
    model: str
    thinking_enabled: bool = False
    page_limit: int = 4
    window_chars: int = 300
    candidates_per_call: int = 8
    semantic_retry_max: int = 3


# Fixed revisions are immutable. Keep their validated adapted content for the worker lifetime,
# across executor windows, while publication guards still check current source validity.
_REVISION_CACHE: OrderedDict[str, dict[str, Any]] = OrderedDict()
_REVISION_CACHE_LIMIT = 4


def model_candidate_payload(proposal: dict) -> dict:
    """Model inputs contain source observations, never the accumulated evidence ledger."""
    keys = {"candidate_id", "text", "context", "source", "original_source", "page_number", "bbox",
            "table", "segment_ids", "content_revision", "units", "unit_work_ids", "evidence_ids",
            "rule", "source_role_hint", "stage", "block_id", "region_id"}
    return {key: value for key, value in proposal.items() if key in keys}


def compact_checkpoint_payload(value: Any) -> Any:
    """Image bytes are rendered again from immutable page/bbox after a restart."""
    if isinstance(value, dict):
        return {key: compact_checkpoint_payload(item) for key, item in value.items() if key != "image_refs"}
    if isinstance(value, tuple | list):
        return [compact_checkpoint_payload(item) for item in value]
    if isinstance(value, str) and value.startswith("data:image/"):
        return {"image_data_hash": hashlib.sha256(value.encode()).hexdigest()}
    return value


def snapshot_rule(snapshot: dict) -> RuleSpec:
    return RuleSpec(**{key: deepcopy(value) for key, value in snapshot.items()
                       if key in RuleSpec.__dataclass_fields__})


def shard_from_dict(row: dict) -> RangeShard:
    return RangeShard(row["strategy_version"], row["range_key"], row["start_page"], row["end_page"],
                      tuple(row.get("page_numbers", [])))


def candidate_payload(row: ParseCandidate, revision_id: UUID) -> dict:
    evidence = row.evidence or {}
    block, region = row.block_ref or {}, row.region_ref or {}
    return {"candidate_id": str(row.candidate_id), "text": row.window or "", "window": row.window or "",
            "context": row.context or "", "source": row.source,
            "original_source": block.get("original_source") or row.source, "stable_key": row.stable_key,
            "material_provenance": block.get("material_provenance"),
            "page_number": block.get("page", region.get("page")), "bbox": region.get("bbox"),
            "table": bool(block.get("table")), "segment_ids": (row.span_ref or {}).get("segments", []),
            "content_revision": str(revision_id), "image_refs": evidence.get("image_refs", []),
            "state": row.state, "evidence": evidence}


def normalize_proposal(value: dict, revision: UUID | str) -> dict:
    payload = cast(dict[str, Any], value.get("payload")) if isinstance(value.get("payload"), dict) else value
    return {**payload, "candidate_id": str(value.get("candidate_id") or payload.get("candidate_id")),
            "source": value.get("source") or payload.get("source", "native"),
            "image_refs": list(value.get("image_refs") or payload.get("image_refs") or []),
            "content_revision": str(payload.get("content_revision") or revision),
            "text": payload.get("text") or payload.get("window") or "",
            "window": payload.get("window") or payload.get("text") or ""}


def native_receipt(candidate: dict) -> dict:
    # Source-role support comes from the platform's document provenance.  The
    # model cannot assign a stronger role (design/measured/etc.) without proof.
    role, basis = source_role(candidate)
    return {"source": "ocr_text" if candidate["source"] == "ocr" else "native_text",
            "content_revision": candidate["content_revision"], "candidate_id": candidate["candidate_id"],
            "segment_ids": candidate["segment_ids"], "text": candidate["text"], "scope": "text",
            # source_role/value_kind are intentionally absent until platform
            # provenance or wording verification supplies a real receipt.
            "attributes": {"source_role": {"status": "supported", "value": role, "basis": basis}}
                          if role else {}}


def source_role(candidate: dict) -> tuple[str | None, str | None]:
    provenance = candidate.get("material_provenance") or {}
    if provenance.get("role") in {"report_body", "report_sheet", "design", "approval", "statement", "ledger"} and provenance.get("quote"):
        return provenance["role"], "source.material_header:" + provenance["quote"]
    text = str(candidate.get("text") or "") + "\n" + str(candidate.get("context") or "")
    for role, markers in (("report_sheet", ("检测报告", "测定报告", "DGC", "检测单", "测定单")),
                          ("report_body", ("报告正文", "评价报告", "评价结果", "评价结论")),
                          ("approval", ("审批意见", "批复文件")),
                          ("statement", ("情况说明", "情况申明")),
                          ("design", ("设计说明", "设计文件", "设计报告")),
                          ("ledger", ("测量台账", "施工台账"))):
        marker = next((value for value in markers if value in text), None)
        if marker:
            return role, "source.wording:" + marker
    return None, None


def parse_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def fact_identity(fact: dict) -> str:
    """Retain equal values for different samples, kinds, units and occurrences."""
    return json.dumps({key: fact.get(key) for key in (
        "candidate_id", "value", "unit", "value_kind", "dimensions", "occurrence_id", "segment_ids")},
        sort_keys=True, ensure_ascii=False, default=str)


def candidate_window_key(candidate: Candidate | dict) -> str:
    value = asdict(candidate) if isinstance(candidate, Candidate) else candidate
    return hashlib.sha256(json.dumps([
        value.get("page_number"), value.get("source"), value.get("bbox"),
        normalize(value.get("text") or value.get("window")),
    ], ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class ReliableFieldExecutor:
    def __init__(self, sessions, gateway: ModelGateway, config: ReliableFieldExecutorConfig, *,
                 content: ContentService | None = None, image_provider: Callable | None = None,
                 image_verifier: Callable | None = None):
        self.sessions, self.gateway, self.config = sessions, gateway, config
        self.content = content or ContentService()
        self.image_provider, self.image_verifier = image_provider, image_verifier

    async def _revision_content(self, db, revision_id):
        key = str(revision_id)
        cached = _REVISION_CACHE.get(key)
        if cached is not None:
            _REVISION_CACHE.move_to_end(key)
            return cached
        pinned = await self.content.read_revision(db, revision_id)
        pages = pinned.content.pages
        material = {}
        for page in pages:
            heading = str(page.get("text") or "")[:512]
            role, basis = source_role({"text": heading})
            if role:
                material[str(page["page_number"])] = {"role": role, "quote": heading, "basis": basis}
        cached = {"pages": pages, "page_index": {p["page_number"]: p for p in pages},
                  "coverage_complete": pinned.content.coverage_complete, "material_provenance": material}
        _REVISION_CACHE[key] = cached
        while len(_REVISION_CACHE) > _REVISION_CACHE_LIMIT:
            _REVISION_CACHE.popitem(last=False)
        return cached

    async def __call__(self, token: LeaseToken, checkpoint: dict) -> QuantumOutcome:
        cp = deepcopy(checkpoint)
        async with self.sessions() as db:
            job = await db.get(ParseFieldJob, token.job_id)
            run = await db.get(ParseRun, token.run_id)
            if not job or not run or job.revision_id != run.content_revision_id:
                return QuantumOutcome("failed_terminal", cp, error="revision_mismatch")
            pinned = await self._revision_content(db, job.revision_id)
            document = await db.get(Document, run.document_id)
            rule = snapshot_rule(cp["rule_snapshot"])
            revision = job.revision_id
            pages = pinned["pages"]
            cp["material_provenance"] = pinned["material_provenance"]
            cp["source_coverage_complete"] = pinned["coverage_complete"]
            cp["hard_limit_tokens"] = (run.extraction_profile or {}).get("hard_limit_tokens")
            pending = list((await db.execute(select(ParseCandidate).where(
                ParseCandidate.job_id == token.job_id, ParseCandidate.state == "pending")
                .order_by(ParseCandidate.stable_key).limit(self.config.candidates_per_call))).scalars().all())
            proposals = [candidate_payload(row, revision) for row in pending]
            for proposal in proposals:
                page = pinned["page_index"].get(proposal.get("page_number"), {})
                segment_ids = proposal.get("segment_ids") or []
                source_block = next((block for block in page.get("text_blocks", [])
                                     if block.get("block_id") and any(str(block["block_id"]) in str(seg) for seg in segment_ids)), None)
                if source_block is not None:
                    match = next((part for part in (proposal.get("segment_ids") or [])
                                  if str(source_block["block_id"]) in str(part)), None)
                    span_start = span_end = None
                    if match and "@" in str(match) and ":" in str(match).rsplit("@", 1)[-1]:
                        try:
                            span_start, span_end = (int(value) for value in str(match).rsplit("@", 1)[-1].split(":", 1))
                        except ValueError:
                            span_start = span_end = None
                    block_text = str(source_block.get("text") or source_block.get("raw_text") or "")
                    if proposal["text"] and block_text.count(proposal["text"]) == 1:
                        span_start = block_text.index(proposal["text"])
                        span_end = span_start + len(proposal["text"])
                    raw = (block_text[span_start:span_end] if span_start is not None and span_end is not None
                           else block_text)
                    proposal.update(block_id=source_block["block_id"], region_id=source_block.get("region_id"),
                                    raw_ocr=raw,
                                    span_start=span_start, span_end=span_end)
            profile = deepcopy(run.extraction_profile or {})
        # The above session has closed before image I/O or the model request.
        if "range_plan" not in cp:
            shards = plan_rule_ranges(pages, rule.semantic_contract)
            if not shards:
                return QuantumOutcome("failed_terminal", cp, error="scope_empty")
            cp.update(range_plan=[asdict(shard) for shard in shards], range_index=0, cursor=None)
            return QuantumOutcome("yielded", cp)
        if proposals:
            return cast(QuantumOutcome, await self._judge(token, cp, proposals, rule, document, pages, profile))
        index, plan = int(cp.get("range_index", 0)), cp["range_plan"]
        if index >= len(plan):
            result = cp.get("semantic_result") or {}
            semantic = result.get("semantic_state")
            if semantic in {"completed", "not_found", "review_required"}:
                return QuantumOutcome("done", cp, semantic_state=semantic)
            return QuantumOutcome("failed_terminal", cp, error="scope_or_evidence_not_closed")
        shard = shard_from_dict(plan[index])
        retrieval = rule.semantic_contract.get("retrieval") or {}
        batch = scan_candidates(FieldSpec.from_rule(rule), pages, shard, cursor=cp.get("cursor"),
                                page_limit=self.config.page_limit, window_chars=self.config.window_chars,
                                content_revision=str(revision), rule_semantic_hash=rule.rule_semantic_hash,
                                formula_patterns=retrieval.get("formula_patterns") or [],
                                table_patterns=retrieval.get("table_patterns") or [])
        cp["scan_batch"] = {"index": index, "candidates": [asdict(item) for item in batch.candidates],
                            "cursor": batch.cursor.as_dict(), "scanned_pages": batch.scanned_pages,
                            "done": batch.done, "unreadable_pages": batch.unreadable_pages}
        cp["cursor"] = None if batch.done else batch.cursor.as_dict()
        cp["range_index"] = index + 1 if batch.done else index
        return QuantumOutcome("yielded", cp)

    async def _judge(self, token, cp, proposals, rule, document, pages, profile):
        tree = CandidateBatchTree(cp.get("candidate_batches"))
        failed_ids = {cid for batch in tree.batches.values() if batch["batch_state"] == "failed"
                      for cid in batch["candidate_ids"]}
        exhausted = [proposal["candidate_id"] for proposal in proposals if proposal["candidate_id"] in failed_ids]
        if exhausted:
            cp.update(unresolved_ids=exhausted, proof_error="failed_candidate_replay_blocked")
            return QuantumOutcome("yielded", cp)
        ready = next((batch for batch in tree.batches.values()
                      if batch["batch_state"] in {"pending", "partially_committed"}), None)
        if ready is not None:
            ids = set(ready["candidate_ids"]) - set(ready["decided_ids"])
            embedded = (ready.get("payload") or {}).get("candidates") or []
            if embedded:
                proposals = [normalize_proposal(dict(item), cp["revision_id"]) for item in embedded
                             if str(item.get("candidate_id")) in ids]
            else:
                proposals = []
            # Fetch all children by stable IDs, including those outside the normal page.
            if not proposals:
                async with self.sessions() as db:
                    rows = (await db.execute(select(ParseCandidate).where(
                        ParseCandidate.job_id == token.job_id, ParseCandidate.candidate_id.in_([UUID(v) for v in ids]),
                        ParseCandidate.state == "pending"))).scalars().all()
                    proposals = [candidate_payload(row, UUID(cp["revision_id"])) for row in rows]
            if not proposals:
                ready["batch_state"] = "done"
                cp["candidate_batches"] = tree.as_dict()
                return QuantumOutcome("yielded", cp)
            batch_id = ready["batch_id"]
        else:
            digest = hashlib.sha256(json.dumps([item["candidate_id"] for item in proposals]).encode()).hexdigest()[:24]
            batch_id = f"batch:{digest}"
            ready = tree.add(batch_id, [item["candidate_id"] for item in proposals])
        registry, inputs, image_inputs = {}, [], {}
        for proposal in proposals:
            eid = "text:" + proposal["candidate_id"]
            registry[eid] = native_receipt(proposal)
            refs = []
            evidence_ids = [eid]
            # Table/OCR evidence always retains its original image.  Absence
            # of a renderer becomes an explicit unresolved object.
            if proposal["source"] == "ocr" or proposal["table"]:
                if self.image_provider is None:
                    cp["unresolved_ids"] = [proposal["candidate_id"]]
                    cp["proof_error"] = "original_image_provider_unavailable"
                    return QuantumOutcome("yielded", cp)
                image, receipt = await self.image_provider(document, pages, proposal)
                ref = "data:image/png;base64," + base64.b64encode(image).decode()
                image_inputs[proposal["candidate_id"]] = (image, ref, proposal)
                refs.append(ref)
                image_id = "image:" + proposal["candidate_id"]
                registry[image_id] = {**receipt, "source": "original_image", "candidate_id": proposal["candidate_id"],
                                      "content_revision": proposal["content_revision"],
                                      "image_ref": {"image_hash": receipt.get("image_hash") or hashlib.sha256(image).hexdigest(),
                                                    "object_key": receipt.get("object_key")}}
                evidence_ids.append(image_id)
            proposal = {**proposal, "evidence_ids": evidence_ids, "rule": rule.semantic_snapshot(),
                        "source_role_hint": source_role(proposal)[0]}
            inputs.append(CandidateInput(proposal["candidate_id"], model_candidate_payload(proposal), tuple(refs), proposal["source"],
                                         tuple(proposal.get("units") or ())))
        retry = int(ready["payload"].get("retry_seq", 0))
        prior = ready["payload"].get("prior_call_id")
        request = CallRequest(job_id=token.job_id, fence=token.fence_token, provider_key=self.config.provider_key,
            dispatch_key=f"{token.job_id}:{batch_id}:{token.fence_token}:{retry}", checkpoint_key=batch_id,
            candidates=tuple(inputs), model=self.config.model, estimated_input_tokens=0,
            retry_seq=retry, retry_of_call_id=UUID(prior) if prior and retry else None,
            hard_limit_tokens=profile.get("hard_limit_tokens"),
            split_path=tuple(ready["split_path"]), thinking_enabled=self.config.thinking_enabled,
            messages=({"role": "system", "content":
                "Extract every matching fact in each candidate under its rule. Return JSON decisions. "
                "Accepted decisions contain value,unit,value_kind,dimensions,source_role from source_role_hint,evidence_ids "
                "from candidate payload; multiple facts in one window use facts array. Reject only when no match. "
                "Quote original wording for kinds and identities; unknown stays unknown. "
                "The complete_scope strategy requires semantic checking even without a label."},))
        if ready["payload"].get("external_quota") and prior:
            response = await self.gateway.requeue_external_quota(request, prior_call_id=prior,
                        dispatch_key=request.dispatch_key + ":quota-resume")
        else:
            response = await self.gateway.execute(request)
        if response.state in {"service_exhausted", "retry_exhausted"}:
            return QuantumOutcome("failed_terminal", cp, error=response.error or response.state)
        if response.wait_state:
            ready["payload"].update(prior_call_id=response.call_id,
                retry_seq=response.retry_seq or retry,
                external_quota=response.wait_state == "waiting_external_quota")
            cp["candidate_batches"] = tree.as_dict()
            return QuantumOutcome(response.wait_state, cp, retry_at=parse_time(response.retry_at), error=response.error)
        for decision in response.decisions:
            cid = decision.get("candidate_id")
            if decision.get("judgement") != "accepted" or cid not in image_inputs:
                continue
            image, ref, proposal = image_inputs[cid]
            verification = (await self.image_verifier(image, proposal) if self.image_verifier is not None
                            else await self.verify_original_image(token, proposal, ref, rule))
            if isinstance(verification, dict) and verification.get("wait_state"):
                cp["candidate_batches"] = tree.as_dict()
                return QuantumOutcome(verification["wait_state"], cp,
                                      retry_at=parse_time(verification.get("retry_at")), error="image_service_wait")
            registry["image:" + cid]["verification"] = verification
            if isinstance(verification, dict) and verification.get("requires_publication"):
                receipt = registry["image:" + cid]
                image_hash = receipt.get("image_hash")
                correction_candidate = {"candidate_id": cid, "page_number": proposal["page_number"],
                    "block_id": proposal.get("block_id"), "region_id": proposal.get("region_id"),
                    "raw_ocr": proposal.get("raw_ocr") or proposal["text"], "image_hash": image_hash,
                    "bbox": receipt.get("bbox") or proposal.get("bbox"), "coordinate_system": "PDF_POINT",
                    "span_start": proposal.get("span_start"), "span_end": proposal.get("span_end"),
                    "corrected_transcription": verification.get("transcription")}
                correction_receipt = {"source": "original_image", "revision_id": proposal["content_revision"],
                    "candidate_id": cid, "region_id": proposal.get("region_id") or proposal.get("block_id"),
                    "image_ref": {"image_hash": image_hash}, "verification": verification}
                cp["ocr_publication"] = {"candidate": correction_candidate, "receipt": correction_receipt}
                return QuantumOutcome("yielded", cp)
        if response.split_batches:
            tree.apply_outcome(batch_id, response)
        elif response.state in {"length", "context_overflow"} or (
                response.state == "invalid" and len(proposals) == 1 and not response.decisions):
            cp["unresolved_ids"] = [item["candidate_id"] for item in proposals]
            ready["batch_state"] = "failed"
            cp["proof_error"] = response.error or "minimum_unit_unresolved"
        else:
            tree.apply_outcome(batch_id, response)
            if response.pending_ids:
                old_retry = int(ready["payload"].get("semantic_retries", 0))
                ready["payload"]["semantic_retries"] = old_retry + 1
                if old_retry + 1 >= max(1, self.config.semantic_retry_max):
                    cp["unresolved_ids"] = response.pending_ids
                    ready["batch_state"] = "failed"
        cp.update(candidate_batches=compact_checkpoint_payload(tree.as_dict()),
                  gateway_outcome=compact_checkpoint_payload(asdict(response)), evidence_registry=registry)
        return QuantumOutcome("yielded", cp)

    async def verify_original_image(self, token, proposal, image_ref, rule):
        """Run a separately accounted transcription call and verify its proposals.

        P10 checks attributes against the observed transcription. Different OCR
        text remains a publication blocker; this routine never edits a pinned
        revision or accepts a model-supplied verification receipt.
        """
        candidate = CandidateInput(proposal["candidate_id"], model_candidate_payload({**proposal, "stage": "original_image_transcription"}),
                                   (image_ref,), "ocr")
        response = await self.gateway.execute(CallRequest(job_id=token.job_id, fence=token.fence_token,
            provider_key=self.config.provider_key,
            dispatch_key=f"image:{token.job_id}:{token.fence_token}:{proposal['candidate_id']}",
            checkpoint_key=f"image:{proposal['candidate_id']}", candidates=(candidate,), model=self.config.model,
            estimated_input_tokens=0, thinking_enabled=self.config.thinking_enabled,
            hard_limit_tokens=None,
            messages=({"role": "system", "content": "Read the attached original image. Return one JSON decision "
                "with candidate_id, judgement accepted, transcription copied exactly, value/unit/value_kind/dimensions "
                "and source_role from document material wording (report_body/report_sheet/design/approval/statement/ledger). "
                "Measured/calculated are value_kind, never source_role. Do not output verified receipts."},)))
        if response.wait_state or not response.decisions:
            return {"status": "unverified", "call_id": response.call_id, "state": response.state,
                    "wait_state": response.wait_state, "retry_at": response.retry_at}
        decision = response.decisions[0]
        transcription = str(decision.get("transcription") or "")
        if not transcription:
            return {"status": "unverified", "call_id": response.call_id}
        observed = {**proposal, "text": transcription, "window": transcription, "source": "native"}
        eid = "visual:" + proposal["candidate_id"]
        receipt = native_receipt(observed)
        decision = {**decision, "evidence_ids": [eid]}
        checked = validate_candidate_decision(decision, observed, rule, {eid: receipt})
        attrs = {}
        for key, assertion in checked.get("assertions", {}).items():
            if assertion.get("status") == "supported":
                attrs[key] = {"status": "supported", "value": assertion.get("observed"),
                              "basis": "wording" if key == "value_kind" else "visual_transcription",
                              "evidence_ids": [eid]}
        return {"status": "verified" if checked.get("usable") else "unverified", "attributes": attrs,
                "transcription": transcription, "call_id": response.call_id,
                "issues": checked.get("issues", []), "requires_publication": checked.get("usable") is True
                    and normalize(proposal.get("raw_ocr") or proposal["text"]) != normalize(transcription)}

    async def before_publish(self, db, token, outcome):
        run = await db.get(ParseRun, token.run_id)
        await content_publish_service.guard_revision(db, document_id=run.document_id,
            revision_id=run.content_revision_id,
            expected_invalidation_watermark=(run.extraction_profile or {}).get("invalidation_watermark"))
        publication = outcome.checkpoint.get("ocr_publication")
        if publication:
            from app.services.execution.field_scheduler import require_lease
            from app.services.execution.job_repository import JobRepository
            job, frozen_run = await JobRepository(db)._locked(token.job_id)
            require_lease(job, frozen_run, token)
            result = await content_publish_service.publish_verified_ocr(db, run_id=token.run_id,
                candidate=publication["candidate"], receipt=publication["receipt"],
                corrected_transcription=publication["candidate"].get("corrected_transcription"),
                request_id=f"ocr:{publication['candidate']['candidate_id']}:{publication['candidate']['image_hash']}")
            if result.get("successor"):
                frozen_run.extraction_profile = {**(frozen_run.extraction_profile or {}),
                    "successor_run_id": str(result["successor"].parse_run_id)}
            return True
        return False

    async def write_result(self, db, job, outcome):
        cp = deepcopy(outcome.checkpoint)
        cp["revision_id"] = str(job.revision_id)
        rule = snapshot_rule(cp["rule_snapshot"])
        repo = CandidateRepository(db)
        plan = [shard_from_dict(item) for item in cp.get("range_plan", [])]
        ranges = await repo.initialize_ranges(job.job_id, plan) if plan else []
        scan = cp.pop("scan_batch", None)
        if scan:
            shard = plan[scan["index"]]
            work = next(item for item in ranges if item.strategy_version == shard.strategy_version and item.range_key == shard.range_key)
            batch = SimpleNamespace(**{**scan, "candidates": [Candidate(**value) for value in scan["candidates"]],
                                       "cursor": ScanCursor.from_dict(scan["cursor"])})
            persisted = await repo.persist_scan(work.work_id, batch)
            failed_windows = set(cp.get("failed_window_keys") or [])
            for row, source in zip(persisted, batch.candidates, strict=True):
                row.block_ref = {**(row.block_ref or {}), "table": source.table,
                    "material_provenance": cp.get("material_provenance", {}).get(str(source.page_number))}
                if row.state == "pending" and candidate_window_key(source) in failed_windows:
                    row.state = "unresolved"
                    row.evidence = {**(row.evidence or {}), "error": "equivalent_failed_window"}
        registry = cp.pop("evidence_registry", {})
        raw = cp.pop("gateway_outcome", {})
        facts = list(cp.get("facts", []))
        fact_keys = {fact_identity(item) for item in facts}
        decisions = []
        for decision in raw.get("decisions", []):
            nested = decision.get("facts") if isinstance(decision, dict) else None
            if isinstance(nested, list) and nested:
                decisions.extend([{**item, "candidate_id": item.get("candidate_id") or decision.get("candidate_id"),
                                   "judgement": item.get("judgement", decision.get("judgement"))}
                                  for item in nested if isinstance(item, dict)])
            else:
                decisions.append(decision)
        existing_registry = {}
        for old in (await db.execute(select(ParseCandidate).where(ParseCandidate.job_id == job.job_id))).scalars().all():
            existing_registry.update((old.evidence or {}).get("registry") or {})
        registry = {**existing_registry, **registry}
        candidate_results = {}
        for decision in decisions:
            row = await db.get(ParseCandidate, UUID(decision["candidate_id"]))
            if row is None or row.job_id != job.job_id or row.state not in {"pending", "accepted", "unresolved"}:
                continue
            proposal = candidate_payload(row, job.revision_id)
            result = validate_candidate_decision(decision, proposal, rule, registry)
            candidate_results.setdefault(str(row.candidate_id), []).append(result)
            row.judgement = decision["judgement"]
            row_registry = {eid: receipt for eid, receipt in registry.items()
                            if receipt.get("candidate_id") == str(row.candidate_id)}
            row.evidence = {"registry": compact_checkpoint_payload(row_registry), "issues": result.get("issues", [])}
            if result.get("fact"):
                fact = result["fact"]
                identity_digest = hashlib.sha256(json.dumps({key: fact.get(key) for key in (
                    "value", "unit", "value_kind", "dimensions")}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]
                fact["occurrence_id"] = str(fact.get("occurrence_id") or fact["candidate_id"]) + ":fact:" + identity_digest
                key = fact_identity(fact)
                if key not in fact_keys:
                    facts.append(fact)
                    fact_keys.add(key)
        for cid, results in candidate_results.items():
            row = await db.get(ParseCandidate, UUID(cid))
            states = {value.get("candidate_state") for value in results}
            row.state = "unresolved" if "unresolved" in states else (
                "accepted" if "accepted" in states else "rejected" if "rejected" in states else "duplicate")
        for cid in cp.pop("unresolved_ids", []):
            row = await db.get(ParseCandidate, UUID(cid))
            if row is not None and row.job_id == job.job_id and row.state in {"pending", "in_flight", "unresolved"}:
                row.state = "unresolved"
                row.evidence = {**(row.evidence or {}), "error": cp.get("proof_error"),
                    "registry": compact_checkpoint_payload({
                        **((row.evidence or {}).get("registry") or {}),
                        **{eid: receipt for eid, receipt in registry.items() if receipt.get("candidate_id") == cid},
                    })}
        await db.flush()
        candidates = list((await db.execute(select(ParseCandidate).where(ParseCandidate.job_id == job.job_id))).scalars().all())
        cp["failed_window_keys"] = sorted({*(cp.get("failed_window_keys") or []),
            *(candidate_window_key(candidate_payload(row, job.revision_id)) for row in candidates if row.state == "unresolved")})
        envelope = {"revision_id": str(job.revision_id), "rule_semantic_hash": job.rule_hash,
            "scope_snapshot": rule.semantic_contract["scope"],
            "expected_keys": [shard.shard_key for shard in plan],
            "items": [{"strategy_version": item.strategy_version, "range_key": item.range_key,
                "scan_done": item.scan_done, "cursor": item.cursor, "quality_blockers": item.quality_blockers,
                "coverage_complete": cp.get("source_coverage_complete") is True} for item in ranges]}
        completion = complete_field(rule, envelope, [candidate_payload(row, job.revision_id) for row in candidates], facts)
        cp.update(facts=facts, semantic_result=completion)
        # Writer changes must become the exact checkpoint passed to Scheduler.
        outcome.checkpoint = cp
        field = await db.scalar(select(ExtractedFieldModel).where(ExtractedFieldModel.parse_run_id == job.run_id,
                                                                 ExtractedFieldModel.field_key == job.field_key))
        if field is None:
            run = await db.get(ParseRun, job.run_id)
            field = ExtractedFieldModel(parse_run_id=job.run_id, document_id=run.document_id,
                field_key=job.field_key, field_name=rule.field_name_cn, extraction_method="reliable_extract",
                data_type=rule.data_type, unit=rule.unit, required=rule.required, rule_version=rule.version)
            db.add(field)
        field.instances = completion["facts"]
        field.field_value = str(completion["facts"][0]["value"]) if completion["facts"] else None
        field.validation_status = completion["semantic_state"]
        field.extraction_detail = {"contract_version": "reliable-extraction-v2", "field_completion": completion,
                                  "semantic_state": completion["semantic_state"], "coverage_complete": completion["coverage_complete"],
                                  "evidence_registry": registry}

