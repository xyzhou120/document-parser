"""共享不可变自动提取结果；附件的运行、复核、失效与版本头各自独立。"""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime
from typing import Iterable
from uuid import UUID, uuid4, uuid5

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.models.attachments import Attachment, AttachmentVersion
from app.models.content import ContentRevision
from app.models.database import Document, ExtractedFieldModel, ParseRun
from app.models.execution import ParseCandidate, ParseFieldJob
from app.models.shared_execution import SharedExecution, SharedExecutionSubscriber

MATERIALIZE_EVENT = "shared_execution.materialize"
SUCCESS = {"COMPLETED", "COMPLETED_WITH_REVIEW"}
TERMINAL = SUCCESS | {"FAILED", "CANCELLED", "SUPERSEDED"}


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def semantic_identity(*, file_hash: str, content_digest: str | None,
                      coverage_digest: str | None, preprocess_version: str,
                      contract_hash: str, model_signature: str,
                      field_contracts: Iterable[tuple[str, str]], strategy: str,
                      prompt_version: str, validator_version: str,
                      token_limit: int | None) -> str:
    return _digest(dict(file_hash=file_hash, content_digest=content_digest,
        coverage_digest=coverage_digest, preprocess_version=preprocess_version,
        contract_hash=contract_hash, model_signature=model_signature,
        field_contracts=sorted((str(k), str(h)) for k, h in field_contracts),
        strategy=strategy, prompt_version=prompt_version,
        validator_version=validator_version, token_limit=token_limit))


async def register_owner(db, *, identity_hash, file_hash, content_digest, coverage_digest,
                         preprocess_version, contract_hash, model_signature,
                         source_run_id, source_version_id, manifest):
    await db.execute(insert(SharedExecution).values(
        identity_hash=identity_hash, file_hash=file_hash, content_digest=content_digest,
        coverage_digest=coverage_digest, preprocess_version=preprocess_version,
        contract_hash=contract_hash, model_signature=model_signature,
        source_run_id=source_run_id, source_version_id=source_version_id,
        state="RUNNING", manifest=manifest).on_conflict_do_nothing(
            index_elements=[SharedExecution.identity_hash]))
    return await db.scalar(select(SharedExecution).where(
        SharedExecution.identity_hash == identity_hash).with_for_update())


async def register_subscriber(db, *, execution, attachment_id, document_id, version_id,
                              parse_run_id, expected_cas, expected_invalidation_watermark,
                              contract_hash, provenance=None):
    row = SharedExecutionSubscriber(shared_execution_id=execution.shared_execution_id,
        target_attachment_id=attachment_id, target_document_id=document_id,
        target_version_id=version_id, parse_run_id=parse_run_id, expected_cas=expected_cas,
        expected_invalidation_watermark=expected_invalidation_watermark,
        requested_contract_hash=contract_hash, state="WAITING",
        source_generation=execution.execution_generation,
        provenance=deepcopy(provenance or {}))
    db.add(row)
    await db.flush()
    return row


async def attach_run(db, run, jobs) -> bool:
    """内容准备完成后按完整键选举独立 owner。返回 True 时业务 run 等待物化。"""
    from app.config import settings
    from app.services.execution import JobRepository
    from app.services.model_gateway import GatewayPolicy
    from app.services.reliable_page_executor import (
        _IMAGE_SYSTEM, _PAGE_SYSTEM, PAGE_PLANNER_VERSION, PAGE_VALIDATION_CACHE_VERSION,
    )
    profile = deepcopy(run.extraction_profile or {})
    if (not settings.agent.shared_execution_enabled or not profile.get("attachment_version_id")
            or profile.get("extraction_strategy") != "page_batch_v1"):
        return False
    if profile.get("shared_execution_id"):
        return True
    version = await db.get(AttachmentVersion, UUID(profile["attachment_version_id"]))
    revision = await db.get(ContentRevision, run.content_revision_id)
    document = await db.get(Document, run.document_id)
    # 仅接受有实际内容与覆盖摘要的修订，不按文件 hash 推测 OCR 等价。
    if not revision or not revision.content_digest or not revision.coverage_digest:
        return False
    policy = {**GatewayPolicy().__dict__, "context_capacity": settings.agent.reliable_context_tokens,
              "max_image_tokens_per_call": settings.agent.reliable_max_image_tokens,
              **(profile.get("gateway_policy") or {})}
    contract = {"rules": sorted((j.field_key, j.rule_hash) for j in jobs),
                "planner": PAGE_PLANNER_VERSION, "page_batch_chars": profile.get("page_batch_chars"),
                "adapter": revision.adapter_version, "preprocess": revision.preprocess_manifest,
                "gateway_policy": policy}
    contract_hash = _digest(contract)
    model_hash = _digest({k: v for k, v in (profile.get("model_config") or {}).items()
                          if k not in {"timeout", "trust_env"}})
    prompt_hash = _digest([_PAGE_SYSTEM, _IMAGE_SYSTEM])
    preprocess_version = _digest([revision.adapter_version, revision.preprocess_manifest])
    identity = semantic_identity(file_hash=document.file_hash,
        content_digest=revision.content_digest, coverage_digest=revision.coverage_digest,
        preprocess_version=preprocess_version, contract_hash=contract_hash,
        model_signature=model_hash, field_contracts=contract["rules"], strategy="page_batch_v1",
        prompt_version=prompt_hash, validator_version=PAGE_VALIDATION_CACHE_VERSION,
        token_limit=profile.get("hard_limit_tokens"))
    # 临时指向本次业务运行；同一事务中替换为独立 owner，其他事务不可见临时身份。
    execution = await register_owner(db, identity_hash=identity, file_hash=document.file_hash,
        content_digest=revision.content_digest, coverage_digest=revision.coverage_digest,
        preprocess_version=preprocess_version, contract_hash=contract_hash,
        model_signature=model_hash, source_run_id=run.parse_run_id, source_version_id=None,
        manifest={**contract, "prompt_hash": prompt_hash, "validator": PAGE_VALIDATION_CACHE_VERSION})
    owner_created = execution.source_run_id == run.parse_run_id
    if owner_created:
        owner_profile = {k: v for k, v in profile.items() if k not in {
            "attachment_version_id", "request_id", "parent_parse_run_id"}}
        owner_profile.update(shared_execution_owner=str(execution.shared_execution_id), gateway_policy=policy)
        owner = ParseRun(parse_run_id=uuid4(), document_id=run.document_id,
            artifact_id=run.artifact_id, content_revision_id=run.content_revision_id,
            contract_version=run.contract_version, execution_generation=0,
            extraction_profile=owner_profile, run_mode="SHARED", status="PLANNED",
            checklist_type=run.checklist_type, requested_field_keys=run.requested_field_keys)
        db.add(owner)
        await db.flush()
        owner_jobs = await JobRepository(db).initialize_jobs(owner.parse_run_id,
            revision_id=run.content_revision_id, fields=[dict(field_key=j.field_key,
                rule_hash=j.rule_hash, snapshot=deepcopy(j.checkpoint["rule_snapshot"])) for j in jobs])
        execution.source_run_id = owner.parse_run_id
        for job in owner_jobs:
            await JobRepository(db).enqueue_outbox(kind="field_job_wakeup", aggregate_id=job.job_id,
                dedupe_key=f"shared-owner:{owner.parse_run_id}:{job.job_id}",
                payload={"run_id": str(owner.parse_run_id), "job_id": str(job.job_id)})
        execution.source_version_id = version.id
    await register_subscriber(db, execution=execution, attachment_id=version.attachment_id,
        document_id=run.document_id, version_id=version.id, parse_run_id=run.parse_run_id,
        expected_cas=version.expected_cas,
        expected_invalidation_watermark=profile.get("invalidation_watermark"),
        contract_hash=contract_hash)
    run.extraction_profile = {**profile, "shared_execution_id": str(execution.shared_execution_id)}
    run.status, version.state = "WAITING_REUSE", "WAITING_REUSE"
    for job in jobs:
        job.execution_state, job.phase = "paused", "waiting_shared_execution"
    if execution.state == "COMPLETED":
        await enqueue_materialize(db, execution, suffix=str(version.id))
    return True


async def enqueue_materialize(db, execution, *, suffix=""):
    from app.services.execution import JobRepository
    await JobRepository(db).enqueue_outbox(kind=MATERIALIZE_EVENT,
        aggregate_id=execution.shared_execution_id,
        dedupe_key=f"shared:{execution.shared_execution_id}:{execution.execution_generation}:{suffix}",
        payload={"shared_execution_id": str(execution.shared_execution_id)})


async def owner_finished(db, run):
    owner_id = (run.extraction_profile or {}).get("shared_execution_owner")
    if not owner_id or run.status not in TERMINAL:
        return
    execution = await db.get(SharedExecution, UUID(owner_id), with_for_update=True)
    if execution.source_run_id != run.parse_run_id:
        return
    execution.state = "COMPLETED" if run.status in SUCCESS else "FAILED"
    execution.completed_at = datetime.utcnow() if run.status in SUCCESS else None
    execution.failure_code = None if run.status in SUCCESS else run.status
    await enqueue_materialize(db, execution)


def _remap(value, replacements):
    """只替换持久化身份，证据正文和模型原文保持逐字不变。"""
    if isinstance(value, dict):
        return {_remap(key, replacements): _remap(item, replacements) for key, item in value.items()}
    if isinstance(value, list):
        return [_remap(item, replacements) for item in value]
    if isinstance(value, str):
        for source, target in replacements.items():
            value = value.replace(source, target)
        return value
    return value


async def materialize_one(sessions, subscriber_id):
    from app.services.attachment_version_service import finalize_version, managed_attachment
    from app.services.content_publish_service import ContentPublicationError, content_publish_service
    from app.services.execution import JobRepository
    async with sessions() as db, db.begin():
        sub0 = await db.get(SharedExecutionSubscriber, subscriber_id)
        if sub0 is None:
            return "missing"
        # 与附件控制保持同一锁序，逐目标小事务物化；重放只读 COMPLETED 标记。
        await db.get(Document, sub0.target_document_id, with_for_update=True)
        attachment = await managed_attachment(db, sub0.target_document_id, lock=True)
        version = await db.get(AttachmentVersion, sub0.target_version_id, with_for_update=True)
        await content_publish_service._head(db, sub0.target_document_id)
        target = await db.get(ParseRun, sub0.parse_run_id, with_for_update=True, populate_existing=True)
        sub = await db.get(SharedExecutionSubscriber, subscriber_id, with_for_update=True, populate_existing=True)
        if sub.state != "WAITING":
            return sub.state
        if version.state == "PAUSED" or target.status == "PAUSED":
            return "paused"
        if version.state in {"CANCELLED", "SUPERSEDED"} or target.status in {"CANCELLED", "SUPERSEDED"}:
            sub.state = "CANCELLED"
            return sub.state
        execution = await db.get(SharedExecution, sub.shared_execution_id)
        source = await db.get(ParseRun, execution.source_run_id)
        if source is None or source.status not in TERMINAL:
            return "waiting_source"
        jobs = list((await db.scalars(select(ParseFieldJob).where(
            ParseFieldJob.run_id == target.parse_run_id).with_for_update())).all())
        if source.status not in SUCCESS:
            # 降级为目标自己的执行。独立 owner 失败不会改写目标的取消或复核状态。
            sub.state, sub.failure_code = "FALLBACK", source.status
            target.status, version.state = "PLANNED", "RUNNING"
            target.extraction_profile = {**(target.extraction_profile or {}),
                                        "shared_fallback_from_run_id": str(source.parse_run_id)}
            for job in jobs:
                job.execution_state, job.phase = "queued", "planned"
                await JobRepository(db).enqueue_outbox(kind="field_job_wakeup", aggregate_id=job.job_id,
                    dedupe_key=f"shared-fallback:{sub.subscriber_id}:{job.job_id}",
                    payload={"run_id": str(target.parse_run_id), "job_id": str(job.job_id)})
            return sub.state
        revision = await db.get(ContentRevision, target.content_revision_id)
        if (revision.content_digest != execution.content_digest
                or revision.coverage_digest != execution.coverage_digest
                or sub.requested_contract_hash != execution.contract_hash):
            sub.state, sub.failure_code = "FAILED", "SHARED_CONTRACT_MISMATCH"
            target.status, version.state = "FAILED", "FAILED"
            return sub.state
        try:
            await content_publish_service.guard_revision(db, document_id=target.document_id,
                revision_id=target.content_revision_id,
                expected_invalidation_watermark=sub.expected_invalidation_watermark)
        except ContentPublicationError:
            sub.state, sub.failure_code = "SUPERSEDED", "TARGET_CONTENT_CHANGED"
            target.status, version.state = "SUPERSEDED", "INCOMPLETE"
            return sub.state
        if attachment.cas_version != sub.expected_cas:
            sub.state, sub.failure_code = "SUPERSEDED", "TARGET_CAS_CHANGED"
            target.status, version.state = "SUPERSEDED", "SUPERSEDED"
            return sub.state
        source_jobs = list((await db.scalars(select(ParseFieldJob).where(
            ParseFieldJob.run_id == source.parse_run_id))).all())
        by_key = {job.field_key: job for job in jobs}
        source_by_id = {job.job_id: job for job in source_jobs}
        if ({(j.field_key, j.rule_hash) for j in source_jobs} != {(j.field_key, j.rule_hash) for j in jobs}
                or any(j.execution_state != "done" for j in source_jobs)):
            raise ValueError("shared owner is not a complete frozen result")
        candidates = list((await db.scalars(select(ParseCandidate).where(
            ParseCandidate.job_id.in_(source_by_id)))).all())
        replacements = {str(source.document_id): str(target.document_id),
            str(source.parse_run_id): str(target.parse_run_id),
            str(source.content_revision_id): str(target.content_revision_id)}
        replacements.update({str(j.job_id): str(by_key[j.field_key].job_id) for j in source_jobs})
        replacements.update({str(c.candidate_id): str(uuid5(by_key[source_by_id[c.job_id].field_key].job_id,
            "shared:" + c.stable_key)) for c in candidates})
        provenance = {"reused_from_run_id": str(source.parse_run_id),
                      "shared_execution_id": str(execution.shared_execution_id)}
        if candidates:
            columns = ("stable_key", "block_ref", "region_ref", "span_ref", "window", "context",
                       "strength", "source", "state", "judgement", "evidence", "split_path")
            values = []
            for candidate in candidates:
                data = {key: (_remap(deepcopy(getattr(candidate, key)), replacements)
                              if key not in {"window", "context"} else getattr(candidate, key)) for key in columns}
                data.update(candidate_id=UUID(replacements[str(candidate.candidate_id)]),
                            job_id=by_key[source_by_id[candidate.job_id].field_key].job_id)
                values.append(data)
            for offset in range(0, len(values), 500):
                statement = insert(ParseCandidate).values(values[offset:offset + 500])
                await db.execute(statement.on_conflict_do_nothing(index_elements=[
                    ParseCandidate.job_id, ParseCandidate.stable_key]))
        fields = list((await db.scalars(select(ExtractedFieldModel).where(
            ExtractedFieldModel.parse_run_id == source.parse_run_id))).all())
        if {f.field_key for f in fields} != set(by_key):
            raise ValueError("shared owner field summary is incomplete")
        for field in fields:
            data = {c.name: _remap(deepcopy(getattr(field, c.name)), replacements)
                    for c in field.__table__.columns if c.name not in {
                        "field_id", "parse_run_id", "document_id", "created_at", "field_value"}}
            data.update(field_id=uuid5(target.parse_run_id, "shared-field:" + field.field_key),
                        parse_run_id=target.parse_run_id, document_id=target.document_id,
                        field_value=field.field_value)
            data["extraction_detail"] = {**(data.get("extraction_detail") or {}), "shared_provenance": provenance}
            await db.execute(insert(ExtractedFieldModel).values(**data).on_conflict_do_nothing(
                index_elements=[ExtractedFieldModel.field_id]))
        for source_job in source_jobs:
            job = by_key[source_job.field_key]
            job.execution_state, job.semantic_state, job.phase = "done", source_job.semantic_state, "finished"
            job.checkpoint = {"rule_snapshot": job.checkpoint["rule_snapshot"], **provenance}
            job.lease_owner = job.lease_until = job.next_attempt_at = None
        target.extraction_profile = {**(target.extraction_profile or {}), **provenance}
        target.started_at = target.started_at or target.created_at
        target.status, version.state = "RUNNING", "RUNNING"
        await db.flush()
        await JobRepository(db).aggregate(target.parse_run_id)
        await finalize_version(db, target)
        sub.state = "COMPLETED" if version.snapshot else "SUPERSEDED"
        sub.completed_at = sub.attached_at = datetime.utcnow()
        sub.provenance = provenance
        return sub.state


async def materialize_execution(sessions, execution_id, *, limit=50):
    async with sessions() as db:
        ids = list((await db.scalars(select(SharedExecutionSubscriber.subscriber_id).where(
            SharedExecutionSubscriber.shared_execution_id == execution_id,
            SharedExecutionSubscriber.state == "WAITING").order_by(
                SharedExecutionSubscriber.created_at).limit(limit))).all())
    states = []
    for subscriber_id in ids:
        states.append(await materialize_one(sessions, subscriber_id))
    return {"subscribers": len(states), "completed": states.count("COMPLETED"), "states": states}


async def recover_shared(sessions, *, limit=50):
    """Beat 从数据库补齐 owner 完成通知和等待目标，发送丢失仍可重放。"""
    async with sessions() as db, db.begin():
        rows = list((await db.scalars(select(SharedExecution).join(ParseRun,
            ParseRun.parse_run_id == SharedExecution.source_run_id).where(
                SharedExecution.state == "RUNNING", ParseRun.status.in_(TERMINAL))
            .limit(limit).with_for_update(of=SharedExecution, skip_locked=True))).all())
        for execution in rows:
            source = await db.get(ParseRun, execution.source_run_id)
            execution.state = "COMPLETED" if source.status in SUCCESS else "FAILED"
            await enqueue_materialize(db, execution)
    async with sessions() as db:
        ids = list((await db.scalars(select(SharedExecutionSubscriber.shared_execution_id).join(
            SharedExecution, SharedExecution.shared_execution_id == SharedExecutionSubscriber.shared_execution_id)
            .where(SharedExecutionSubscriber.state == "WAITING", SharedExecution.state.in_(["COMPLETED", "FAILED"]))
            .distinct().limit(limit))).all())
    for identity in ids:
        await materialize_execution(sessions, identity, limit=limit)
    return len(ids)
