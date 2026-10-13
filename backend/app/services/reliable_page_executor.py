"""Page-batched reliable extraction.

This executor is deliberately a thin companion to :mod:`reliable_field_executor`.
The legacy executor owns the evidence rules and the publication guards; this module
only changes the unit of work from ``field x range`` to ``page x all fields``.
Keeping the page pass here makes the old strategy fully backwards compatible while
allowing a run to share one model request between all of its fields.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
from uuid import UUID, uuid5

from sqlalchemy import select

from app.models.database import Document, ExtractedFieldModel, ParseRun
from app.models.execution import ParseCandidate, ParseFieldJob, ParseModelCall
from app.services.candidate_retrieval import NUMERIC_TYPES, normalize
from app.services.evidence_verification import validate_candidate_decision, value_in_text
from app.services.execution import LeaseToken, QuantumOutcome
from app.services.field_completion import complete_field
from app.services.model_gateway import CallRequest, CandidateInput, GatewayOutcome
from app.services.page_extraction import build_page_request, build_page_units
from app.services.page_table_evidence import table_fact_support
from app.services.reliable_field_executor import (
    ReliableFieldExecutor,
    ReliableFieldExecutorConfig,
    compact_checkpoint_payload,
    native_receipt,
    parse_time,
    snapshot_rule,
)
from app.services.retrieval_plan import plan_rule_ranges


def _page_candidate_id(job_id: UUID, unit_id: str) -> UUID:
    return uuid5(job_id, "page-batch:" + str(unit_id))


def _unit_key(unit: dict) -> str:
    return str(unit.get("unit_id") or f"page:{unit.get('page_number')}")


def _scope_pages(pages: list[dict], rule) -> set[int]:
    return {number for shard in plan_rule_ranges(pages, rule.semantic_contract)
            for number in shard.page_numbers}


def _split_text_unit(unit: dict, depth: int) -> list[dict]:
    """Split at line boundaries, retaining source identity and context."""
    lines = str(unit.get("text") or "").splitlines()
    if len(lines) < 2:
        return []
    middle = len(lines) // 2
    children = []
    parts = ("\n".join(lines[:middle]), "\n".join(lines[middle:]))
    for ordinal, text in enumerate(parts):
        if not text.strip():
            continue
        child = {**unit, "text": text,
                 "unit_id": f"{_unit_key(unit)}:s{depth}{ordinal}",
                 "context": str(unit.get("context") or "") + ("\n" + parts[0][-400:] if ordinal else "")}
        children.append(child)
    return children


def _split_rule_unit(unit: dict, rules: list, depth: int) -> list[dict]:
    if len(rules) < 2:
        return []
    middle = len(rules) // 2
    groups = (rules[:middle], rules[middle:])
    return [{**unit, "unit_id": f"{_unit_key(unit)}:r{depth}{ordinal}",
             "field_keys": [rule.field_key for rule in group]}
            for ordinal, group in enumerate(groups) if group]


async def _deadline(awaitable, remaining: float):
    if remaining <= 0:
        if hasattr(awaitable, "close"):
            awaitable.close()
        raise TimeoutError("page_batch_deadline_exceeded")
    async with asyncio.timeout(remaining):
        return await awaitable


def _remaining_seconds(cp: dict, profile: dict) -> float:
    started = parse_time(cp["page_started_at"])
    if started is None:
        raise ValueError("page batch start time is required")
    return int(profile.get("max_run_seconds") or 300) - (datetime.utcnow() - started).total_seconds()


def _page_rule(rule) -> dict:
    """Send semantics once; persistence/audit fields add no extraction knowledge."""
    snapshot = rule.semantic_snapshot()
    keys = {"field_key", "field_name_cn", "data_type", "unit", "value_kind",
            "dimension_schema", "field_description", "cardinality", "identity_dimensions",
            "applicable_dimensions"}
    result = {key: value for key, value in snapshot.items() if key in keys and value is not None}
    applicable = set(snapshot.get("applicable_dimensions") or [])
    if "dimension_schema" in result:
        result["dimension_schema"] = {key: value for key, value in result["dimension_schema"].items()
                                      if key in applicable}
    result.pop("dimensions", None)  # Identical names already exist in the schema/contract.
    knowledge = {key: value for key, value in ((snapshot.get("strategy") or {}).get("agent_extract") or {}).items()
                 if key in {"instructions", "synonyms", "negative_examples", "source_roles", "background"}}
    if knowledge:
        result["strategy"] = {"agent_extract": knowledge}
    return result


class ReliablePageExecutor(ReliableFieldExecutor):
    """One durable page request shared by every field in a parse run.

    The scheduler should claim only the coordinator job.  ``write_result`` is
    intentionally run in that job's guarded transaction and writes the sibling
    field jobs as well, so a worker restart cannot leave a half-completed page.
    """

    def __init__(self, sessions, gateway, config: ReliableFieldExecutorConfig, **kwargs):
        super().__init__(sessions, gateway, config, **kwargs)

    async def __call__(self, token: LeaseToken, checkpoint: dict) -> QuantumOutcome:
        cp = deepcopy(checkpoint)
        async with self.sessions() as db:
            job = await db.get(ParseFieldJob, token.job_id)
            run = await db.get(ParseRun, token.run_id)
            if job is None or run is None or job.revision_id != run.content_revision_id:
                return QuantumOutcome("failed_terminal", cp, error="revision_mismatch")
            profile = dict(run.extraction_profile or {})
            if profile.get("extraction_strategy") != "page_batch_v1":
                return await super().__call__(token, checkpoint)
            jobs = list((await db.execute(select(ParseFieldJob).where(
                ParseFieldJob.run_id == token.run_id).order_by(ParseFieldJob.field_key))).scalars().all())
            coordinator = str(profile.get("coordinator_field_key") or min(j.field_key for j in jobs))
            if job.field_key != coordinator:
                return QuantumOutcome("failed_terminal", cp, error="non_coordinator_page_job")
            pinned = await self._revision_content(db, job.revision_id)
            pages = pinned["pages"]
            units = list(cp.get("page_units") or build_page_units(
                pages, max_chars=int(profile.get("page_batch_chars") or 6000)))
            rules = [snapshot_rule(dict(j.checkpoint or {}).get("rule_snapshot") or {}) for j in jobs]
            rules_by_key = {rule.field_key: rule for rule in rules}
            scopes = {rule.field_key: _scope_pages(pages, rule) for rule in rules}
            cp["field_scopes"] = {key: sorted(numbers) for key, numbers in scopes.items()}
            cp.setdefault("page_units", units)
            cp.setdefault("page_index", 0)
            cp.setdefault("failed_units", [])
            cp.setdefault("page_results", [])
            started_at = getattr(run, "started_at", None)
            cp.setdefault("page_started_at", started_at.isoformat() if started_at else datetime.utcnow().isoformat())
            cp["field_keys"] = sorted(rules_by_key)
            cp["revision_id"] = str(job.revision_id)
            cp["material_provenance"] = pinned["material_provenance"]
            cp["source_coverage_complete"] = pinned["coverage_complete"]
            cp["hard_limit_tokens"] = profile.get("hard_limit_tokens")
            index = int(cp.get("page_index", 0))
            saved_units = {_unit_key(result.get("unit") or {}) for result in cp.get("page_results", [])}
            while index < len(units) and _unit_key(units[index]) in saved_units:
                index += 1
            cp["page_index"] = index
            self._set_progress(cp)
            if index >= len(units):
                # The writer performs final completion.  Its result is read back
                # from the checkpoint so the scheduler can close the coordinator.
                semantic = str(cp.get("semantic_state") or "not_found")
                if cp.get("failed_units") or any(state not in {"completed", "not_found", "review_required"}
                        for state in (cp.get("semantic_states") or {job.field_key: semantic}).values()):
                    return QuantumOutcome("failed_terminal", cp, error="page_batch_units_unresolved")
                return QuantumOutcome("done", cp, semantic_state=semantic)
            started = parse_time(cp["page_started_at"])
            if started is None:
                return QuantumOutcome("failed_terminal", cp, error="page_batch_start_time_missing")
            remaining = int(profile.get("max_run_seconds") or 300) - (datetime.utcnow() - started).total_seconds()
            if remaining <= 0:
                return QuantumOutcome("failed_terminal", cp, error="page_batch_deadline_exceeded")
            unit = dict(units[index])
            unit_id = _unit_key(unit)
            coordinator_cid = str(_page_candidate_id(job.job_id, unit_id))
            applicable_rules = [rule for rule in rules if unit.get("page_number") in scopes[rule.field_key]
                and (not unit.get("field_keys") or rule.field_key in unit["field_keys"])]
            if not applicable_rules or not str(unit.get("text") or "").strip():
                cp["page_results"].append({"unit": unit, "candidate_id": coordinator_cid,
                    "response": {"state": "succeeded", "decisions": [
                        {"candidate_id": coordinator_cid, "judgement": "rejected"}]}})
                cp["page_index"] = index + 1
                self._set_progress(cp)
                return QuantumOutcome("yielded", cp)
            payload = build_page_request(unit, [_page_rule(rule) for rule in applicable_rules], coordinator_cid)
            # Receipts are platform-owned; IDs in the model payload are only
            # references and are never trusted as evidence.
            payload["candidate_id"] = coordinator_cid
            payload["content_revision"] = str(job.revision_id)
            payload["evidence_ids"] = ["page:" + coordinator_cid]
            payload["material_provenance"] = cp["material_provenance"].get(str(unit["page_number"]))
            payload["source_role_hint"] = (payload["material_provenance"] or {}).get("role")
            document = await db.get(Document, run.document_id)
            retry_generation = int(cp.get("unit_retry_generations", {}).get(unit_id, 0))
            dispatch_key = f"{job.job_id}:page:{unit_id}:g{retry_generation}:{int(cp.get('page_retry', 0))}"
            prior_dispatch = await db.scalar(select(ParseModelCall).where(
                ParseModelCall.dispatch_key == dispatch_key))
            request_fence = prior_dispatch.fence if prior_dispatch else token.fence_token
        refs = []
        image_receipt = None
        if unit.get("source") == "ocr":
            if self.image_provider is None:
                cp["page_results"].append({"unit": unit, "candidate_id": coordinator_cid,
                    "response": {"state": "invalid", "error": "original_image_provider_unavailable"}})
                cp["failed_units"] = sorted({*cp["failed_units"], unit_id})
                cp["page_index"] = index + 1
                self._set_progress(cp)
                return QuantumOutcome("yielded", cp)
            try:
                image, image_receipt = await _deadline(self.image_provider(document, pages, {**unit,
                    "candidate_id": coordinator_cid, "content_revision": cp["revision_id"]}), _remaining_seconds(cp, profile))
            except TimeoutError:
                return self._timeout_outcome(cp, index, unit, coordinator_cid)
            image_receipt = {**image_receipt, "image_hash": image_receipt.get("image_hash")
                or hashlib.sha256(image).hexdigest()}
            refs.append("data:image/png;base64," + base64.b64encode(image).decode())
        candidate = CandidateInput(coordinator_cid, payload, tuple(refs), str(unit.get("source") or "native"))
        retry = int(cp.get("page_retry", 0))
        prior = cp.get("prior_call_id")
        request = CallRequest(
                job_id=job.job_id, fence=request_fence, provider_key=self.config.provider_key,
                dispatch_key=dispatch_key,
                checkpoint_key=f"page:{unit_id}", candidates=(candidate,), model=self.config.model,
                estimated_input_tokens=0, retry_seq=int(cp.get("page_retry", 0)),
                retry_of_call_id=UUID(prior) if prior else None,
                hard_limit_tokens=profile.get("hard_limit_tokens"),
                thinking_enabled=self.config.thinking_enabled,
                messages=({"role": "system", "content":
                    "Extract every matching fact for every supplied field_rule on this page. "
                    "Return JSON decisions with candidate_id, judgement, and facts. Every fact "
                    "must include field_key, value, unit, value_kind, dimensions, source_role, "
                    "and evidence_ids copied from the supplied candidate. Preserve all repeated "
                    "measurements; do not invent facts or IDs."},),
            )
        saved_extraction = cp.get("pending_extraction") or {}
        if saved_extraction.get("unit_id") == unit_id:
            response = GatewayOutcome(**saved_extraction["response"])
        elif cp.get("external_quota") and prior:
            try:
                response = await _deadline(self.gateway.requeue_external_quota(request, prior_call_id=prior,
                    dispatch_key=request.dispatch_key + ":quota-resume"), _remaining_seconds(cp, profile))
            except TimeoutError:
                return self._timeout_outcome(cp, index, unit, coordinator_cid)
        else:
            try:
                response = await _deadline(self.gateway.execute(request), _remaining_seconds(cp, profile))
            except TimeoutError:
                return self._timeout_outcome(cp, index, unit, coordinator_cid)
        if response.wait_state:
            cp["page_retry"] = response.retry_seq or retry
            cp["prior_call_id"] = response.call_id
            cp["external_quota"] = response.wait_state == "waiting_external_quota"
            cp["last_gateway"] = compact_checkpoint_payload(asdict(response))
            return QuantumOutcome(response.wait_state, cp, retry_at=parse_time(response.retry_at),
                                  error=response.error)
        if response.state in {"service_exhausted", "retry_exhausted"}:
            cp["failed_units"] = sorted({*cp.get("failed_units", []), unit_id})
            cp["page_retry"] = 0
            return QuantumOutcome("failed_terminal", cp, error=response.error or response.state)
        if response.state == "invalid" and response.call_id and not cp.get("invalid_retried", {}).get(unit_id):
            cp.setdefault("invalid_retried", {})[unit_id] = True
            cp.update(page_retry=retry + 1, prior_call_id=response.call_id, external_quota=False)
            return QuantumOutcome("yielded", cp)
        if response.state in {"context_overflow", "length"}:
            depth = int(cp.get("split_depths", {}).get(unit_id, 0))
            children = _split_rule_unit(unit, applicable_rules, depth) if depth < 6 else []
            if not children and depth < 6:
                children = _split_text_unit(unit, depth)
            if children:
                cp["page_units"] = units[:index] + children + units[index + 1:]
                cp["page_retry"] = 0
                for key in ("prior_call_id", "external_quota", "pending_extraction"):
                    cp.pop(key, None)
                cp.setdefault("split_depths", {}).update({_unit_key(child): depth + 1 for child in children})
                cp.setdefault("split_history", []).append({"parent": unit_id,
                    "children": [_unit_key(child) for child in children], "reason": response.state})
                self._set_progress(cp)
                return QuantumOutcome("yielded", cp)
            return self._failed_unit(cp, index, unit, coordinator_cid, response)
        verification = None
        if refs and any(decision.get("judgement") == "accepted" for decision in response.decisions):
            cp["pending_extraction"] = {"unit_id": unit_id, "response": compact_checkpoint_payload(asdict(response))}
            verification_key = f"{job.job_id}:page-image:{unit_id}:g{retry_generation}:{int(cp.get('image_retry', 0))}"
            async with self.sessions() as db:
                previous_image_call = await db.scalar(select(ParseModelCall).where(
                    ParseModelCall.dispatch_key == verification_key))
            image_request = CallRequest(job_id=job.job_id,
                fence=previous_image_call.fence if previous_image_call else token.fence_token,
                provider_key=self.config.provider_key, dispatch_key=verification_key,
                checkpoint_key=f"page-image:{unit_id}", model=self.config.model,
                estimated_input_tokens=0, thinking_enabled=self.config.thinking_enabled,
                hard_limit_tokens=profile.get("hard_limit_tokens"),
                retry_seq=int(cp.get("image_retry", 0)),
                retry_of_call_id=UUID(cp["image_prior_call_id"]) if cp.get("image_prior_call_id") else None,
                candidates=(CandidateInput(coordinator_cid, {**unit, "stage": "original_image_transcription"},
                    tuple(refs), "ocr"),),
                messages=({"role": "system", "content": "Read the attached original image independently. "
                    "Return one JSON decision with the exact candidate_id, judgement accepted and transcription "
                    "copied from the image. Preserve table headers, rows, units and object identities. "
                    "Do not infer, correct to match previous extraction, or output verified receipts."},))
            try:
                if cp.get("image_external_quota") and cp.get("image_prior_call_id"):
                    image_response = await _deadline(self.gateway.requeue_external_quota(image_request,
                        prior_call_id=cp["image_prior_call_id"], dispatch_key=verification_key + ":quota-resume"), _remaining_seconds(cp, profile))
                else:
                    image_response = await _deadline(self.gateway.execute(image_request), _remaining_seconds(cp, profile))
            except TimeoutError:
                return self._timeout_outcome(cp, index, unit, coordinator_cid)
            if image_response.wait_state:
                cp.update(image_retry=image_response.retry_seq or int(cp.get("image_retry", 0)),
                    image_prior_call_id=image_response.call_id,
                    image_external_quota=image_response.wait_state == "waiting_external_quota")
                return QuantumOutcome(image_response.wait_state, cp,
                    retry_at=parse_time(image_response.retry_at), error=image_response.error)
            transcription = next((str(d.get("transcription") or d.get("value") or "") for d in image_response.decisions
                if d.get("judgement") == "accepted"), "")
            verification = {"transcription": transcription, "call_id": image_response.call_id,
                            "receipt": image_receipt}
            if not transcription:
                # The page extraction itself succeeded. Failed independent
                # verification leaves its facts explicitly unusable for review;
                # it must not erase them or invalidate unrelated absent fields.
                verification["error"] = image_response.error or "image_transcription_unavailable"
                cp["review_units"] = sorted({*cp.get("review_units", []), unit_id})
            for key in ("pending_extraction", "image_retry", "image_prior_call_id", "image_external_quota"):
                cp.pop(key, None)
        # Save the complete response before advancing the cursor.  On replay the
        # scheduler can write this result without issuing another provider call.
        cp["page_results"].append({"unit": unit, "candidate_id": coordinator_cid,
                                    "response": compact_checkpoint_payload(asdict(response)),
                                    "image_verification": verification})
        cp["page_index"] = index + 1
        cp["page_retry"] = 0
        cp.pop("prior_call_id", None)
        cp.pop("external_quota", None)
        if response.state in {"invalid", "length", "context_overflow"} or response.pending_ids or not response.decisions:
            cp["failed_units"] = sorted({*cp.get("failed_units", []), unit_id})
        for decision in response.decisions:
            if decision.get("judgement") != "accepted":
                continue
            nested = decision.get("facts")
            if not isinstance(nested, list) or not nested or any(not isinstance(fact, dict)
                    or fact.get("field_key") not in {rule.field_key for rule in applicable_rules} for fact in nested):
                cp["failed_units"] = sorted({*cp.get("failed_units", []), unit_id})
        self._set_progress(cp)
        if cp["page_index"] < len(cp["page_units"]):
            return QuantumOutcome("yielded", cp)
        # The writer calculates each field's semantic state.  Keep the run open
        # for that transaction, then close it on the next coordinator quantum.
        return QuantumOutcome("yielded", cp)

    @staticmethod
    def _failed_unit(cp, index, unit, candidate_id, response) -> QuantumOutcome:
        cp["failed_units"] = sorted({*cp.get("failed_units", []), _unit_key(unit)})
        cp["page_results"].append({"unit": unit, "candidate_id": candidate_id,
            "response": compact_checkpoint_payload(asdict(response))})
        cp["page_index"] = index + 1
        ReliablePageExecutor._set_progress(cp)
        return QuantumOutcome("yielded", cp)

    @staticmethod
    def _timeout_outcome(cp, index, unit, candidate_id) -> QuantumOutcome:
        cp["deadline_error"] = "page_batch_deadline_exceeded"
        outcome = ReliablePageExecutor._failed_unit(cp, index, unit, candidate_id,
            GatewayOutcome("timeout", error="page_batch_deadline_exceeded"))
        outcome.state, outcome.error = "failed_terminal", "page_batch_deadline_exceeded"
        return outcome

    @staticmethod
    def _set_progress(cp):
        units = list(cp.get("page_units") or [])
        processed = units[:int(cp.get("page_index", 0))]
        cp["page_progress"] = {"processed_units": len(processed), "total_units": len(units),
            "processed_pages": len({u["page_number"] for u in processed
                if not any(v["page_number"] == u["page_number"] for v in units[len(processed):])}),
            "total_pages": len({u["page_number"] for u in units}),
            "failed_units": len(cp.get("failed_units") or []), "strategy": "page_batch_v1"}

    async def write_result(self, db, job, outcome):
        """Persist all page candidates/facts and close sibling field jobs atomically."""
        cp = deepcopy(outcome.checkpoint)
        run = await db.get(ParseRun, job.run_id, with_for_update=True)
        if run is None:
            raise KeyError(job.run_id)
        siblings = list((await db.execute(select(ParseFieldJob).where(
            ParseFieldJob.run_id == job.run_id).order_by(ParseFieldJob.field_key))).scalars().all())
        rules = {s.field_key: snapshot_rule(dict(s.checkpoint or {}).get("rule_snapshot") or {}) for s in siblings}
        units = list(cp.get("page_units") or [])
        results_by_unit = {_unit_key(row.get("unit") or {}): row for row in cp.get("page_results", [])}
        # Each field gets one candidate per page.  Stable keys make recovery and
        # duplicate writes harmless.
        by_field: dict[str, list[ParseCandidate]] = {}
        for sibling in siblings:
            rows = list((await db.execute(select(ParseCandidate).where(
                ParseCandidate.job_id == sibling.job_id))).scalars().all())
            existing = {row.stable_key: row for row in rows}
            out = []
            for unit in units:
                if unit.get("page_number") not in set(cp.get("field_scopes", {}).get(sibling.field_key, [])):
                    continue
                if unit.get("field_keys") and sibling.field_key not in unit["field_keys"]:
                    continue
                uid = _unit_key(unit)
                key = f"page_batch:{uid}"
                row = existing.get(key)
                if row is None:
                    row = ParseCandidate(candidate_id=_page_candidate_id(sibling.job_id, uid), job_id=sibling.job_id,
                        stable_key=key, block_ref={"page": unit.get("page_number"), "table": bool(unit.get("table")),
                            "material_provenance": cp.get("material_provenance", {}).get(str(unit.get("page_number")))},
                        region_ref={"page": unit.get("page_number"), "bbox": unit.get("bbox")},
                        span_ref={"segments": list(unit.get("segment_ids") or [])},
                        window=str(unit.get("text") or ""), context=str(unit.get("context") or ""),
                        source=str(unit.get("source") or "native"), state="pending")
                    db.add(row)
                out.append(row)
            by_field[sibling.field_key] = out
        await db.flush()
        finalize = outcome.state in {"done", "failed_terminal"}
        all_results: dict[str, dict] = {}
        for field_key, rule in rules.items():
            candidates = by_field[field_key]
            facts: list[dict] = []
            field_registry = {}
            for row in candidates:
                uid = row.stable_key.removeprefix("page_batch:")
                page_result = results_by_unit.get(uid)
                source = {"candidate_id": str(row.candidate_id), "text": row.window or "",
                          "window": row.window or "", "context": row.context or "",
                          "source": row.source, "page_number": (row.block_ref or {}).get("page"),
                          "bbox": (row.region_ref or {}).get("bbox"), "table": bool((row.block_ref or {}).get("table")),
                          "segment_ids": (row.span_ref or {}).get("segments", []),
                          "content_revision": str(job.revision_id), "stable_key": row.stable_key,
                          "material_provenance": (row.block_ref or {}).get("material_provenance")}
                registry_id = "page:" + str(row.candidate_id)
                registry = {registry_id: native_receipt(source)}
                row.evidence = {"registry": registry}
                decisions = []
                if page_result:
                    response = page_result.get("response") or {}
                    for decision in response.get("decisions") or []:
                        nested = decision.get("facts") if isinstance(decision, dict) else None
                        if isinstance(nested, list):
                            decisions.extend([{**fact, "field_key": fact.get("field_key") or decision.get("field_key"),
                                "candidate_id": str(row.candidate_id), "judgement": fact.get("judgement", decision.get("judgement")),
                                "evidence_ids": [registry_id]} for fact in nested
                                if isinstance(fact, dict) and fact.get("field_key") == field_key])
                        elif decision.get("field_key") == field_key:
                            decisions.append({**decision, "candidate_id": str(row.candidate_id),
                                "evidence_ids": [registry_id]})
                if uid in set(cp.get("failed_units") or []):
                    row.state, row.judgement = "unresolved", None
                    row.evidence = {**row.evidence, "error": "page_unit_failed"}
                elif decisions:
                    row.judgement = "accepted"
                    checked_states = []
                    for ordinal, decision in enumerate(decisions):
                        quote = str(decision.get("quote") or "")
                        table_support = table_fact_support(page_result["unit"], decision, rule) if source["table"] else {}
                        quote_valid = bool(quote and normalize(quote) in normalize(source["text"]) and
                            value_in_text(decision.get("value"), quote, rule.data_type in NUMERIC_TYPES))
                        if source["table"] and "value" in table_support.get("attributes", {}):
                            quote_valid = True
                            quote = str((decision.get("table_relation") or {}).get("row_quote") or quote)
                            decision = {**decision, "quote": quote}
                            header_id = f"table:{row.candidate_id}:{ordinal}"
                            registry[header_id] = {**native_receipt(source), "scope": "header",
                                "text": source["text"], "attributes": table_support["attributes"]}
                            decision["evidence_ids"] = [registry_id, header_id]
                        fact_source = {**source, "text": quote, "window": quote,
                            "context": source["text"] + "\n" + source["context"]} if quote_valid else source
                        image_info = (page_result or {}).get("image_verification") or {}
                        transcription = str(image_info.get("transcription") or "")
                        if transcription:
                            # One independent transcription supports every fact
                            # on this page. Attribute claims are checked by the
                            # same platform validator against that observation;
                            # model-supplied verification receipts are ignored.
                            observed = {**source, "text": transcription, "window": transcription,
                                        "source": "native", "table": False}
                            visual_id = f"visual:{row.candidate_id}:{ordinal}"
                            visual_receipt = native_receipt(observed)
                            visual_checked = validate_candidate_decision(
                                {**decision, "evidence_ids": [visual_id]}, observed, rule,
                                {visual_id: visual_receipt})
                            attrs = {key: {"status": "supported", "value": attr["observed"],
                                "basis": "wording" if key == "value_kind" else "visual_transcription"}
                                for key, attr in visual_checked.get("assertions", {}).items()
                                if attr.get("status") == "supported"}
                            if source["table"]:
                                independent_table = table_fact_support(
                                    {"text": transcription, "transcription": transcription}, decision, rule)
                                for key, attr in table_support.get("attributes", {}).items():
                                    independent_attr = independent_table.get("attributes", {}).get(key)
                                    if independent_attr and independent_attr.get("value") == attr.get("value"):
                                        attrs[key] = attr
                            image_id = f"image:{row.candidate_id}:{ordinal}"
                            receipt = image_info.get("receipt") or {}
                            numeric = rule.data_type in NUMERIC_TYPES
                            visual_supports_fact = bool(
                                normalize(str(decision.get("quote") or "")) in normalize(transcription)
                                or value_in_text(decision.get("value"), transcription, numeric)
                            )
                            registry[image_id] = {"source": "original_image",
                                "content_revision": str(job.revision_id), "candidate_id": str(row.candidate_id),
                                "image_ref": {"image_hash": receipt.get("image_hash")},
                                "verification": {"status": "verified", "attributes": attrs,
                                    "call_id": image_info.get("call_id")},
                                # A full-page OCR string and a transcription will
                                # naturally differ in whitespace, table markup and
                                # layout. Publication is required only when the
                                # fact itself is contradicted or absent from the
                                # independent image observation.
                                "requires_publication": not visual_supports_fact}
                            decision = {**decision, "evidence_ids": [registry_id, image_id]}
                        checked = validate_candidate_decision(decision, fact_source, rule, registry)
                        if not quote_valid:
                            checked["usable"] = False
                            checked["candidate_state"] = "unresolved"
                            if checked.get("fact"):
                                checked["fact"]["usable"] = False
                                checked["fact"]["assertions"]["value"]["status"] = "unknown"
                        checked_states.append(checked.get("candidate_state"))
                        if checked.get("fact"):
                            fact = checked["fact"]
                            fact["occurrence_id"] = f"{row.stable_key}:fact:{ordinal}"
                            facts.append(fact)
                    row.state = "unresolved" if "unresolved" in checked_states else "accepted"
                    row.evidence = {"registry": registry}
                elif page_result and _unit_key(page_result.get("unit") or {}) in set(cp.get("failed_units") or []):
                    row.state, row.judgement = "unresolved", None
                    row.evidence = {**row.evidence, "error": "page_unit_failed"}
                elif page_result:
                    row.state, row.judgement = "rejected", "rejected"
                else:
                    row.state, row.judgement = "pending", None
                field_registry.update((row.evidence or {}).get("registry") or {})
            sibling = next(s for s in siblings if s.field_key == field_key)
            field_units = [unit for unit in units
                if unit.get("page_number") in set(cp.get("field_scopes", {}).get(field_key, []))
                and (not unit.get("field_keys") or field_key in unit["field_keys"])]
            envelope = {"revision_id": str(job.revision_id), "rule_semantic_hash": sibling.rule_hash,
                "scope_snapshot": rule.semantic_contract["scope"],
                "expected_keys": [f"page_batch_v1:page:{_unit_key(u)}" for u in field_units],
                "items": [{"strategy_version": "page_batch_v1", "range_key": f"page:{_unit_key(u)}",
                           "scan_done": _unit_key(u) in results_by_unit and _unit_key(u) not in set(cp.get("failed_units") or []),
                           "quality_blockers": [], "coverage_complete": cp.get("source_coverage_complete") is True,
                           "coverage_status": "complete"} for u in field_units]}
            completion = complete_field(rule, envelope,
                [{"candidate_id": str(row.candidate_id), "state": row.state, "content_revision": str(job.revision_id),
                  "page_number": (row.block_ref or {}).get("page")} for row in candidates], facts)
            if (completion["semantic_state"] == "partial_candidates" and completion["range_closed"]
                    and not any(row.state == "pending" for row in candidates)
                    and not any(_unit_key(unit) in set(cp.get("failed_units") or []) for unit in field_units)):
                # Extraction has ended, but uncertain attributes require a human
                # decision. Keep all support failures and unusable facts visible;
                # never label this automatic_complete or claim proven absence.
                completion["semantic_state"] = "review_required"
                completion["risks"].append({"code": "AUTOMATIC_EVIDENCE_UNVERIFIED"})
            all_results[field_key] = completion
            field = await db.scalar(select(ExtractedFieldModel).where(
                ExtractedFieldModel.parse_run_id == job.run_id,
                ExtractedFieldModel.field_key == field_key))
            if field is None:
                field = ExtractedFieldModel(parse_run_id=job.run_id, document_id=run.document_id,
                    field_key=field_key, field_name=rule.field_name_cn, extraction_method="page_batch_v1",
                    data_type=rule.data_type, unit=rule.unit, required=rule.required, rule_version=rule.version)
                db.add(field)
            field.instances = completion["facts"]
            field.field_value = str(completion["facts"][0]["value"]) if completion["facts"] else None
            field.validation_status = completion["semantic_state"]
            field.extraction_detail = {"contract_version": "reliable-extraction-v2",
                "field_completion": completion, "semantic_state": completion["semantic_state"],
                "coverage_complete": completion["coverage_complete"], "evidence_registry": field_registry}
        cp["semantic_states"] = {key: value["semantic_state"] for key, value in all_results.items()}
        cp["semantic_state"] = all_results.get(job.field_key, {}).get("semantic_state", "not_found")
        if outcome.state == "done":
            if any(state not in {"completed", "not_found", "review_required"} for state in cp["semantic_states"].values()):
                outcome.state, outcome.error = "failed_terminal", "page_batch_units_unresolved"
            else:
                outcome.semantic_state = cp["semantic_state"]
        outcome.checkpoint = cp
        # The coordinator's transition is applied by JobRepository. Siblings are
        # closed here under the same run lock; incomplete work is terminally failed.
        for sibling in siblings:
            state = all_results[sibling.field_key]["semantic_state"]
            if not finalize:
                continue
            sibling.semantic_state = state
            if sibling.job_id == job.job_id:
                # The guarded scheduler transition still owns the coordinator's
                # running -> done/failed lease transition.
                continue
            sibling.phase = "finished" if state in {"completed", "not_found", "review_required"} else "failed"
            sibling.execution_state = "done" if state in {"completed", "not_found", "review_required"} else "failed_terminal"
            sibling.lease_owner = sibling.lease_until = None
            sibling.next_attempt_at = None
        await db.flush()

