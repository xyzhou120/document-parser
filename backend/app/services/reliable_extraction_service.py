"""P13-A 基于数据库的恢复与状态门面。

本服务有意在执行账本处停止。语义事实发布仍由 P10/P11/P12 负责，
本门面不能将其表示为成功提取。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import Document, DocumentArtifact, ParseRun
from app.models.execution import ExecutionOutbox, ParseFieldJob
from app.services.execution import (
    DispatchOutbox,
    FieldSchedulerRunner,
    JobRepository,
    aggregate_run_status,
)
from app.services.execution.field_scheduler import QuantumExecutor

CONTRACT_VERSION = "reliable-extraction-v2"


class ReliableExecutionError(RuntimeError):
    pass


class ContentPreparationRequired(ReliableExecutionError):
    def __init__(self, build_id: UUID):
        super().__init__("content preparation queued")
        self.build_id = build_id


class ReliableExtractionService:
    def __init__(self):
        # P13-B 在工作进程启动时安装 P06/P07/P10 语义执行器。
        # 缺失状态必须显式表达，不能伪造完成结果。
        self.executor_factory: Callable[[Any], QuantumExecutor] | None = None

    async def start(self, db: AsyncSession, document_id: UUID, *, rules: list[Any],
                    revision_id: UUID | None = None, parent_run_id: UUID | None = None,
                    document_token_hard_limit: int | None = None, request_id: str | None = None,
                    extraction_strategy: str | None = None) -> ParseRun:
        """创建新的显式 v2 运行，并冻结规则、内容和模型快照。"""
        from app.config import settings
        from app.services.content_publish_service import content_publish_service

        document = await db.scalar(select(Document).where(Document.document_id == document_id)
                                   .with_for_update().execution_options(populate_existing=True))
        if document is None:
            raise KeyError(document_id)
        model = settings.models["vision_llm"]
        frozen_profile = {"contract_version": CONTRACT_VERSION, "extraction_engine": "reliable",
            "rules": [asdict(rule) for rule in rules], "model_config": {
                "base_url": model.base_url, "model_name": model.model_name, "model_version": model.model_version,
                "timeout": model.timeout, "trust_env": model.trust_env,
                "thinking_enabled": settings.agent.thinking_enabled},
            "gateway_policy": {}, "hard_limit_tokens": document_token_hard_limit}
        strategy = extraction_strategy or settings.agent.reliable_strategy
        if strategy not in {"page_batch_v1", "field_retrieval_v2"}:
            raise ReliableExecutionError("unsupported reliable extraction strategy")
        if strategy == "page_batch_v1":
            frozen_profile.update(extraction_strategy=strategy,
                coordinator_field_key=min((rule.field_key for rule in rules), default=""),
                max_run_seconds=settings.agent.reliable_max_seconds,
                page_batch_chars=settings.agent.page_batch_chars,
                hard_limit_tokens=document_token_hard_limit if document_token_hard_limit is not None
                                  else settings.agent.reliable_token_limit)
        frozen_profile["parent_parse_run_id"] = str(parent_run_id) if parent_run_id else None
        frozen_profile["request_id"] = request_id
        frozen_profile["document_file_hash"] = document.file_hash
        if request_id:
            existing = await db.scalar(select(ParseRun).where(ParseRun.document_id == document_id,
                ParseRun.contract_version == CONTRACT_VERSION,
                ParseRun.extraction_profile["request_id"].as_string() == request_id).order_by(ParseRun.created_at.desc()).limit(1))
            if existing is not None:
                previous = existing.extraction_profile or {}
                keys = ("rules", "model_config", "hard_limit_tokens", "parent_parse_run_id", "document_file_hash",
                        "extraction_strategy", "page_batch_chars", "max_run_seconds")
                if any(previous.get(key) != frozen_profile.get(key) for key in keys):
                    raise ReliableExecutionError("request_id already binds a different frozen extraction request")
                if revision_id is not None and existing.content_revision_id != revision_id:
                    raise ReliableExecutionError("request_id already binds a different content revision")
                return existing
        if not rules:
            raise ReliableExecutionError("no selected rules")
        head = await content_publish_service._head(db, document_id)
        if revision_id is not None and head.active_content_revision_id != revision_id:
            raise ReliableExecutionError("requested content revision is not active")
        if head.active_content_revision_id is None:
            artifact = await db.scalar(select(DocumentArtifact).where(
                DocumentArtifact.document_id == document_id, DocumentArtifact.status.in_(["READY", "PARTIAL"]))
                .order_by(DocumentArtifact.created_at.desc()).limit(1))
            if artifact is None:
                from app.services.content_preparation_service import content_preparation_service
                build = await content_preparation_service.prepare_build(db, document_id,
                    profile=frozen_profile, request_id=request_id or f"reliable:{document_id}:{uuid4()}")
                raise ContentPreparationRequired(UUID(build["build_id"]))
            from app.services.content_adapter import ContentAdapter
            if not ContentAdapter().adapt(artifact).coverage_complete:
                from app.services.content_preparation_service import content_preparation_service
                build = await content_preparation_service.prepare_build(db, document_id,
                    profile=frozen_profile, request_id=request_id or f"reliable:{document_id}:{uuid4()}")
                raise ContentPreparationRequired(UUID(build["build_id"]))
            published = await content_publish_service.publish_artifact(db, artifact,
                expected_parent_revision_id=None, expected_cas_version=head.cas_version,
                expected_invalidation_watermark=head.invalidation_watermark)
            head = published["head"]
        pinned_id = head.active_content_revision_id
        from app.models.content import ContentRevision
        pinned_revision = await db.get(ContentRevision, pinned_id)
        from app.services.content_service import ContentService
        pinned_content = await ContentService().read_revision(db, pinned_id)
        if not pinned_content.content.coverage_complete:
            from app.services.content_preparation_service import content_preparation_service
            build = await content_preparation_service.prepare_build(db, document_id,
                profile=frozen_profile, request_id=request_id or f"reliable:{document_id}:{uuid4()}",
                parent_revision_id=pinned_id)
            raise ContentPreparationRequired(UUID(build["build_id"]))
        parent = await db.get(ParseRun, parent_run_id) if parent_run_id else None
        if parent_run_id and (parent is None or parent.document_id != document_id):
            raise ReliableExecutionError("parent run belongs to another document")
        profile = {**frozen_profile, "invalidation_watermark": head.invalidation_watermark,
                   "content_revision_id": str(pinned_id)}
        if pinned_revision is None:
            raise ReliableExecutionError("pinned content revision is unavailable")
        if not rules:
            raise ReliableExecutionError("no selected rules")
        run = ParseRun(parse_run_id=uuid4(), document_id=document_id, parent_parse_run_id=parent_run_id,
            artifact_id=pinned_revision.artifact_id,
            content_revision_id=pinned_id, contract_version=CONTRACT_VERSION, execution_generation=0,
            status="PLANNED", run_mode="FULL", checklist_type=document.checklist_type, extraction_profile=profile,
            requested_field_keys=[rule.field_key for rule in rules])
        db.add(run)
        await db.flush()
        repo = JobRepository(db)
        jobs = await repo.initialize_jobs(run.parse_run_id, revision_id=pinned_id, fields=[
            {"field_key": rule.field_key, "rule_hash": rule.rule_semantic_hash, "snapshot": asdict(rule)} for rule in rules])
        for job in jobs:
            await repo.enqueue_outbox(kind="field_job_wakeup", aggregate_id=job.job_id,
                dedupe_key=f"{job.job_id}:initial", payload={"run_id": str(run.parse_run_id), "job_id": str(job.job_id)})
        return run

    async def _run(self, db: AsyncSession, run_id: UUID) -> ParseRun:
        run = await db.get(ParseRun, run_id)
        if run is None:
            raise KeyError(f"parse run {run_id} not found")
        return run

    def ensure_contract(self, run: ParseRun) -> None:
        if run.contract_version != CONTRACT_VERSION:
            raise ReliableExecutionError(
                "该运行不是 reliable-extraction-v2；旧 legacy 运行请使用兼容查询，不允许混入新账本"
            )

    async def initialize_run(self, db: AsyncSession, run_id: UUID, *, revision_id: UUID,
                             fields: list[dict[str, Any]]) -> list[ParseFieldJob]:
        run = await self._run(db, run_id)
        self.ensure_contract(run)
        if run.content_revision_id != revision_id:
            raise ReliableExecutionError("run content revision does not match frozen job revision")
        return await JobRepository(db).initialize_jobs(run_id, revision_id=revision_id, fields=fields)

    async def execution(self, db: AsyncSession, run_id: UUID, *, limit: int = 100,
                        cursor: str | None = None) -> dict[str, Any]:
        run = await self._run(db, run_id)
        if run.contract_version != CONTRACT_VERSION:
            return {
                "contract_version": "legacy-compatible-read",
                "run_id": run_id,
                "document_id": run.document_id,
                "status": run.status,
                "execution_generation": run.execution_generation,
                "automatic_complete": False,
                "total_fields": 0,
                "counts": {},
                "jobs": [],
                "page_progress": None,
                "next_cursor": None,
                "legacy": True,
            }
        query = select(ParseFieldJob).where(ParseFieldJob.run_id == run_id).order_by(ParseFieldJob.field_key)
        if cursor:
            query = query.where(ParseFieldJob.field_key > cursor)
        page_size = max(1, min(limit, 500))
        fetched = list((await db.execute(query.limit(page_size + 1))).scalars().all())
        rows = fetched[:page_size]
        all_jobs = list((await db.execute(select(ParseFieldJob).where(ParseFieldJob.run_id == run_id))).scalars().all())
        status = aggregate_run_status(run, all_jobs)
        counts = Counter(job.execution_state for job in all_jobs)
        counts.update(f"semantic:{job.semantic_state}" for job in all_jobs)
        page_progress = None
        profile = dict(getattr(run, "extraction_profile", None) or {})
        if profile.get("extraction_strategy") == "page_batch_v1" and all_jobs:
            coordinator_key = str(profile.get("coordinator_field_key") or min(job.field_key for job in all_jobs))
            coordinator = next((job for job in all_jobs if job.field_key == coordinator_key), None)
            checkpoint = dict(coordinator.checkpoint or {}) if coordinator is not None else {}
            raw_progress = checkpoint.get("page_progress")
            if isinstance(raw_progress, dict):
                page_progress = {
                    "processed_units": max(0, int(raw_progress.get("processed_units") or 0)),
                    "total_units": max(0, int(raw_progress.get("total_units") or 0)),
                    "processed_pages": max(0, int(raw_progress.get("processed_pages") or 0)),
                    "total_pages": max(0, int(raw_progress.get("total_pages") or 0)),
                    "failed_units": max(0, int(raw_progress.get("failed_units") or 0)),
                    "strategy": str(raw_progress.get("strategy") or "page_batch_v1"),
                }
        return {
            "contract_version": CONTRACT_VERSION,
            "run_id": run_id,
            "document_id": run.document_id,
            "status": status,
            "execution_generation": run.execution_generation,
            "automatic_complete": status == "COMPLETED",
            "total_fields": len(all_jobs),
            "counts": dict(counts),
            "jobs": rows,
            "page_progress": page_progress,
            "next_cursor": rows[-1].field_key if len(fetched) > page_size else None,
            "legacy": False,
        }

    async def field_jobs(self, db: AsyncSession, run_id: UUID, *, limit: int = 100,
                         cursor: str | None = None) -> dict[str, Any]:
        payload = await self.execution(db, run_id, limit=limit, cursor=cursor)
        return {key: payload[key] for key in ("contract_version", "run_id", "jobs", "next_cursor", "legacy")}

    async def control(self, db: AsyncSession, run_id: UUID, action: str, *, retry_failed: bool = False,
                      document_token_hard_limit: int | None = None) -> dict[str, Any]:
        run = await self._run(db, run_id)
        self.ensure_contract(run)
        if document_token_hard_limit is not None:
            previous = (run.extraction_profile or {}).get("hard_limit_tokens")
            if action != "resume" or previous is None or document_token_hard_limit <= previous:
                raise ValueError("resume may only increase an existing explicit hard limit")
        status = await JobRepository(db).control_run(run_id, action, retry_failed=retry_failed)
        if document_token_hard_limit is not None:
            profile = deepcopy(run.extraction_profile or {})
            audit = list(profile.get("hard_limit_changes") or [])
            audit.append({"previous": previous, "new": document_token_hard_limit,
                          "at": datetime.utcnow().isoformat(),
                          "execution_generation": run.execution_generation})
            run.extraction_profile = {**profile, "hard_limit_tokens": document_token_hard_limit,
                                      "hard_limit_changes": audit}
        return {"contract_version": CONTRACT_VERSION, "run_id": run_id, "status": status,
                "execution_generation": run.execution_generation, "resumed": action == "resume"}

    async def dispatch_due(self, db: AsyncSession, *, limit: int = 100) -> int:
        """只重新入队到期任务；不调用模型，也不伪造成功。"""
        return await JobRepository(db).requeue_due(limit=limit)

    async def process_run(self, sessions: Any, run_id: UUID, *, executor: QuantumExecutor,
                          owner: str, max_quantums: int = 20, writer: Any = None,
                          before_publish: Any = None,
                          lease_seconds: int = 90) -> dict[str, Any]:
        """执行一个任务量子窗口，并自动将剩余持久化工作入队。

        调用方提供真实的语义执行器。这也是故障注入边界：测试使用受控结果，
        生产环境的 P13-B 提供检索、模型和核验，而不改变恢复逻辑。
        """
        if max_quantums < 1:
            raise ValueError("max_quantums must be positive")
        async with sessions() as db:
            run = await self._run(db, run_id)
            self.ensure_contract(run)
        runner = FieldSchedulerRunner(sessions)
        executed = 0
        while executed < max_quantums:
            token = await runner.run_next(run_id=run_id, owner=owner, execute=executor,
                                          write_result=writer, before_publish=before_publish,
                                          lease_seconds=lease_seconds)
            if token is None:
                break
            executed += 1
        async with sessions() as db, db.begin():
            repo = JobRepository(db)
            current = await db.get(ParseRun, run_id)
            head = None
            if before_publish is not None and current.status not in {"CANCELLED", "SUPERSEDED", "PAUSED"}:
                from app.services.content_publish_service import content_publish_service
                head = await content_publish_service.guard_revision(db, document_id=current.document_id,
                    revision_id=current.content_revision_id,
                    expected_invalidation_watermark=(current.extraction_profile or {}).get("invalidation_watermark"))
            status = await repo.aggregate(run_id)
            if head is not None and status == "COMPLETED":
                await content_publish_service.publish_parse_run(db, run_id,
                    expected_cas_version=head.cas_version,
                    expected_invalidation_watermark=head.invalidation_watermark,
                    expected_current_parse_run_id=head.current_parse_run_id)
            jobs = list((await db.execute(select(ParseFieldJob).where(ParseFieldJob.run_id == run_id))).scalars().all())
            runnable = [job for job in jobs if job.execution_state in {"queued", "yielded"}]
            # 每次 yield 已在同一事务中写入一个持久化字段唤醒事件。
            # 不要在此追加窗口级广播；调度器会按 run_id 合并待处理唤醒事件。
            if runnable:
                pending_wake = await db.scalar(select(ExecutionOutbox.event_id).where(
                    ExecutionOutbox.kind == "field_job_wakeup",
                    ExecutionOutbox.aggregate_id.in_([job.job_id for job in jobs]),
                    ExecutionOutbox.dispatch_state.in_(["pending", "failed"]),
                ).limit(1))
                # 只包含已完成字段的窗口不会产生 yield 唤醒事件。
                # 使用一个事件保留剩余可运行工作。
                if pending_wake is None:
                    await repo.enqueue_outbox(kind="field_job_wakeup", aggregate_id=runnable[0].job_id,
                        dedupe_key=f"run:{run_id}:continue:{current.execution_generation}:{sum(job.attempt for job in jobs)}:{sum(job.progress_seq for job in jobs)}",
                        payload={"run_id": str(run_id), "execution_generation": current.execution_generation})
            return {"status": status, "run_id": str(run_id), "quantums_executed": executed,
                    "remaining_runnable": len(runnable),
                    "successor_run_id": (current.extraction_profile or {}).get("successor_run_id")}

    async def dispatch_events(self, sessions: Any, sender: Callable[[str, dict[str, Any]], Awaitable[None]],
                              *, limit: int = 100, kinds: list[str] | None = None,
                              run_id: UUID | None = None) -> int:
        """领取并提交，在 SQL 之外发送，然后在短事务中确认。"""
        async with sessions() as db, db.begin():
            dispatcher = DispatchOutbox(db)
            await dispatcher.recover_stuck(before=datetime.utcnow() - timedelta(minutes=5), limit=limit,
                                           kinds=kinds or ["field_job_wakeup"])
            if kinds is None or "field_job_wakeup" in kinds:
                await JobRepository(db).requeue_due(limit=limit)
            aggregate_ids = None
            if run_id is not None:
                aggregate_ids = list((await db.execute(select(ParseFieldJob.job_id).where(
                    ParseFieldJob.run_id == run_id))).scalars().all())
            selected_kinds = kinds or ["field_job_wakeup"]
            rows = []
            if "field_job_wakeup" in selected_kinds:
                rows.extend(await dispatcher.claim_field_runs(limit=limit, run_id=run_id))
            other_kinds = [kind for kind in selected_kinds if kind != "field_job_wakeup"]
            if other_kinds and len(rows) < limit:
                rows.extend(await dispatcher.claim_due(limit=limit - len(rows), kinds=other_kinds,
                                                       aggregate_ids=aggregate_ids))
            events = [(row.event_id, row.kind, dict(row.payload), row.attempts) for row in rows]
        sent = 0
        for event_id, kind, payload, attempt in events:
            error = None
            try:
                await sender(kind, payload)
            except Exception as exc:
                error = str(exc)
            async with sessions() as db, db.begin():
                row = await db.get(ExecutionOutbox, event_id, with_for_update=True)
                # 延迟的旧调度器不能覆盖后续重新投递尝试的确认结果。
                if row is None or row.dispatch_state != "sending" or row.attempts != attempt:
                    continue
                if error:
                    await DispatchOutbox(db).mark_failed(row, error)
                else:
                    await DispatchOutbox(db).mark_sent(row)
                    sent += 1
        return sent


reliable_extraction_service = ReliableExtractionService()
