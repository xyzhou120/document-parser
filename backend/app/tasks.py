"""
Celery任务定义
"""

import logging
from uuid import UUID

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.celery_app import celery_app
from app.config import settings
from app.services import ParseService


def _retryable(exc: Exception) -> bool:
    try:
        from app.services.errors import is_retryable_error

        return bool(is_retryable_error(exc))
    except ImportError:
        return isinstance(exc, TimeoutError | ConnectionError | OSError)


async def _set_run_status(parse_run_id: UUID, status: str, message: str) -> None:
    """Mark an exhausted task without reusing a loop-bound pool."""
    from app.models.database import ParseRun

    engine = create_async_engine(settings.database.url, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as db:
            run = await db.get(ParseRun, parse_run_id)
            if run:
                run.status = status
                run.error_message = message
                await db.commit()
    finally:
        await engine.dispose()


logger = logging.getLogger(__name__)


def content_build_failure_allowed(job, *, worker_id, initial_fence, file_hash, error, now):
    """Check failure-write authority without revoking another delivery's lease."""
    from app.services.content_build_service import ContentBuildError
    if isinstance(error, ContentBuildError) and (
        "already leased" in str(error) or "stale content build worker" in str(error)
    ):
        return False
    if job is None or job.phase == "published" or job.file_hash != file_hash or initial_fence is None:
        return False
    owns_claim = job.lease_owner == worker_id and job.fence_token == initial_fence + 1
    before_claim = job.fence_token == initial_fence and (
        job.lease_until is None or job.lease_until <= now
    )
    return owns_claim or before_claim


@celery_app.task(name="parse_document", bind=True, max_retries=3)
def parse_document_task(self, document_id: str, parse_run_id: str, field_keys: list[str] | None = None, force_refresh: bool = False):
    """
    解析文档的Celery任务

    Args:
        document_id: 文档ID（字符串格式）
        parse_run_id: 解析运行ID（字符串格式）
    """
    import asyncio

    logger.info(f"Starting parse task: document_id={document_id}, parse_run_id={parse_run_id}")

    # A valid run identifier lets us record a permanent document-input failure.
    # Invalid run identifiers cannot identify a database record to update.
    run_id = UUID(parse_run_id)
    try:
        doc_id = UUID(document_id)
        # 执行解析
        parse_service = ParseService()

        async def run_parse():
            # 每次 asyncio.run 都创建新事件循环，不能复用上一轮的 asyncpg 连接池。
            engine = create_async_engine(settings.database.url, poolclass=NullPool)
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            try:
                async with sessions() as db:
                    await parse_service.parse_document(db, doc_id, run_id, field_keys=field_keys, force_refresh=force_refresh)
            finally:
                await engine.dispose()

        # 运行异步任务
        asyncio.run(run_parse())

        logger.info(f"Parse task completed: parse_run_id={parse_run_id}")
        return {"status": "success", "parse_run_id": parse_run_id}

    except Exception as e:
        logger.error(f"Parse task failed: {e}", exc_info=True)
        if _retryable(e) and self.request.retries < self.max_retries:
            # Keep the state visible while Celery schedules the next attempt.
            try:
                asyncio.run(_set_run_status(run_id, "RETRYING", str(e)))
            except Exception:
                logger.warning("无法写入 RETRYING 状态", exc_info=True)
            raise self.retry(exc=e, countdown=min(60 * (2**self.request.retries), 600)) from e
        try:
            asyncio.run(_set_run_status(run_id, "FAILED", str(e)))
        except Exception:
            logger.warning("无法写入最终 FAILED 状态", exc_info=True)
        raise


@celery_app.task(name="reliable_execution_wakeup", bind=True, max_retries=3)
def reliable_execution_wakeup_task(self, parse_run_id: str, dispatch_event_id: str | None = None,
                                 dispatch_attempt: int | None = None):
    """Execute bounded real retrieval/model/validation quanta on the v2 queue."""
    import asyncio

    if not settings.app.reliable_enabled:
        return {"status": "disabled", "parse_run_id": parse_run_id}

    async def wake():
        from pathlib import Path
        from tempfile import TemporaryDirectory

        import httpx
        import redis.asyncio as redis
        from openai import AsyncOpenAI

        from app.rules.strategies.llm import LLMStrategy
        from app.services.call_ledger import CallLedger
        from app.services.evidence_images import EvidenceImages
        from app.services.execution import JobRepository
        from app.services.model_gateway import GatewayPolicy, ModelGateway
        from app.services.reliable_extraction_service import reliable_extraction_service
        from app.services.reliable_field_executor import (
            ReliableFieldExecutor,
            ReliableFieldExecutorConfig,
        )
        from app.services.service_circuit import RedisCircuitStore, ServiceCircuit, service_key

        engine = create_async_engine(settings.database.url, poolclass=NullPool)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        client = None
        redis_client = redis.from_url(settings.redis.url)
        try:
            run_id = UUID(parse_run_id)
            if dispatch_event_id is not None:
                async with sessions() as db, db.begin():
                    consumed = await JobRepository(db).consume_run_wakeup(
                        run_id, UUID(dispatch_event_id), dispatch_attempt=dispatch_attempt,
                        task_id=str(self.request.id))
                if not consumed:
                    return {"status": "duplicate_or_stale_wakeup", "run_id": parse_run_id}
            async with sessions() as db:
                run = await reliable_extraction_service._run(db, run_id)
                reliable_extraction_service.ensure_contract(run)
                if run.status in {"CANCELLED", "SUPERSEDED", "PAUSED", "COMPLETED", "COMPLETED_WITH_REVIEW", "FAILED"}:
                    return {"status": run.status, "run_id": parse_run_id}
                profile = run.extraction_profile or {}
                model = profile.get("model_config") or {}
            config = settings.models["vision_llm"]
            client = AsyncOpenAI(base_url=LLMStrategy._normalize_base(model.get("base_url", config.base_url)),
                                 api_key=config.api_key or "dummy", timeout=model.get("timeout", config.timeout),
                                 max_retries=0,
                                 http_client=httpx.AsyncClient(trust_env=model.get("trust_env", config.trust_env)))
            circuit = ServiceCircuit(RedisCircuitStore(redis_client))
            ledger = CallLedger(sessions)
            gateway = ModelGateway(client, ledger, circuit, GatewayPolicy(**profile.get("gateway_policy", {})))
            with TemporaryDirectory(prefix="reliable-evidence-") as directory:
                renderers = {}

                async def images(document, pages, candidate):
                    key = (str(document.document_id), document.file_hash)
                    if key not in renderers:
                        workspace = Path(directory) / str(document.document_id)
                        workspace.mkdir()
                        renderers[key] = EvidenceImages(document, pages, workspace)
                    return await renderers[key].get_with_receipt(
                        candidate["page_number"], candidate.get("bbox"))

                executor_class = ReliableFieldExecutor
                if profile.get("extraction_strategy") == "page_batch_v1":
                    from app.services.reliable_page_executor import ReliablePageExecutor
                    executor_class = ReliablePageExecutor
                executor = executor_class(sessions, gateway,
                    ReliableFieldExecutorConfig(provider_key=service_key("model", model.get("base_url", config.base_url),
                        model.get("model_name", config.model_name), model.get("model_version", config.model_version)),
                        model=model.get("model_name", config.model_name),
                        thinking_enabled=model.get("thinking_enabled", True)), image_provider=images)
                result = await reliable_extraction_service.process_run(sessions, run_id, executor=executor,
                    writer=executor.write_result, before_publish=executor.before_publish,
                    # Keep a wake short so one long document cannot monopolize
                    # the solo local worker while other runs wait in Redis.
                    owner=f"celery:{self.request.id}", max_quantums=3, lease_seconds=300)
                if result.get("remaining_runnable"):
                    try:
                        await asyncio.to_thread(reliable_execution_dispatch_task.delay)
                    except Exception:
                        # Durable wakes and beat still recover broker downtime.
                        logger.warning("Reliable continuation waits for beat recovery")
                return result
        finally:
            if client is not None:
                await client.close()
            await redis_client.aclose()
            await engine.dispose()

    return asyncio.run(wake())


@celery_app.task(name="reliable_execution_dispatch", max_retries=0)
def reliable_execution_dispatch_task():
    """Periodic lost-message, expired lease and service retry recovery."""
    import asyncio
    if not settings.app.reliable_enabled:
        return {"status": "disabled"}

    async def dispatch():
        from app.services.reliable_extraction_service import reliable_extraction_service
        engine = create_async_engine(settings.database.url, poolclass=NullPool)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async def sender(kind, payload):
                if kind == "content.build.execute":
                    await asyncio.to_thread(reliable_content_build_task.delay, payload["build_id"])
                    return
                if kind != "field_job_wakeup":
                    raise ValueError("unsupported execution event")
                await asyncio.to_thread(reliable_execution_wakeup_task.delay, payload["run_id"],
                    payload.get("dispatch_event_id"), payload.get("dispatch_attempt"))
            sent = await reliable_extraction_service.dispatch_events(sessions, sender,
                                        kinds=["field_job_wakeup", "content.build.execute"])
            return {"events_sent": sent}
        finally:
            await engine.dispose()
    return asyncio.run(dispatch())


@celery_app.task(name="reliable_content_build", bind=True, max_retries=3)
def reliable_content_build_task(self, build_id: str):
    import asyncio
    if not settings.app.reliable_enabled:
        return {"status": "disabled"}
    async def build():
        from datetime import datetime

        from app.models.content import ContentBuildJob
        from app.models.database import Document
        from app.services.content_preparation_service import content_preparation_service
        engine = create_async_engine(settings.database.url, poolclass=NullPool)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        worker_id = f"celery:{self.request.id}"
        initial_fence = None
        initial_file_hash = None
        try:
            async with sessions() as db:
                current = await db.get(ContentBuildJob, UUID(build_id))
                initial_fence = current.fence_token if current else None
                initial_file_hash = current.file_hash if current else None
            result = await content_preparation_service.run_build(sessions, UUID(build_id),
                worker_id=worker_id, max_regions=20)
            if result.get("run_id"):
                await asyncio.to_thread(reliable_execution_wakeup_task.delay, result["run_id"])
            return result
        except Exception as exc:
            from app.models.content import ContentBuildJob
            from app.models.database import Document
            async with sessions() as db, db.begin():
                job = await db.get(ContentBuildJob, UUID(build_id), with_for_update=True, populate_existing=True)
                document = await db.get(Document, job.document_id) if job is not None else None
                now = datetime.utcnow()
                allowed = content_build_failure_allowed(job, worker_id=worker_id,
                    initial_fence=initial_fence, file_hash=initial_file_hash, error=exc, now=now)
                if allowed and document and document.file_hash == initial_file_hash:
                    job.dispatch_state = "partial"
                    job.last_error = f"content_preparation_failed:{type(exc).__name__}"
                    job.lease_owner = None
                    job.lease_until = None
            raise
        finally:
            await engine.dispose()
    return asyncio.run(build())


@celery_app.task(name="assistant_message_execute", bind=True, max_retries=3)
def assistant_message_execute_task(self, message_id: str):
    """Run one queued assistant message on its dedicated worker queue."""
    import asyncio
    if not settings.app.assistant_enabled:
        return {"status": "disabled", "message_id": message_id}

    async def execute():
        from app.services.assistant.index_repository import IndexRepository
        from app.services.assistant.model_providers import providers_from_settings
        from app.services.assistant.qa_service import (
            OpenAIAnswerer,
            QARepository,
            QAService,
            run_message,
        )
        from app.services.assistant.retrieval_service import RetrievalService
        engine = create_async_engine(settings.database.url, poolclass=NullPool)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            # QAService owns its repository/generation fence transaction.
            config = next((settings.models.get(key) for key in ("text_llm", "llm", "vision_llm")
                           if settings.models.get(key)), None)
            repository = IndexRepository(sessions)
            providers = providers_from_settings(settings)
            service = QAService(QARepository(sessions), repository,
                retrieval=RetrievalService(repository, embedding_provider=providers.embedding,
                                            reranker=providers.reranker),
                answerer=OpenAIAnswerer(config) if config else None)
            return await run_message(UUID(message_id), service=service)
        finally:
            await engine.dispose()
    return asyncio.run(execute())


@celery_app.task(name="assistant_outbox_dispatch", max_retries=0)
def assistant_outbox_dispatch_task():
    """Dispatch assistant events through the shared durable outbox."""
    import asyncio
    if not settings.app.assistant_enabled:
        return {"status": "disabled"}

    async def dispatch():
        from app.services.reliable_extraction_service import reliable_extraction_service
        engine = create_async_engine(settings.database.url, poolclass=NullPool)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async def sender(kind, payload):
                if kind == "assistant.card.build":
                    await asyncio.to_thread(assistant_card_build_task.delay, payload["card_id"])
                    return
                if kind == "content_index_build":
                    await asyncio.to_thread(content_index_build_task.delay, payload["revision_id"])
                    return
                if kind != "assistant.message.execute":
                    raise ValueError("unsupported assistant event")
                await asyncio.to_thread(assistant_message_execute_task.delay, payload["message_id"])
            sent = await reliable_extraction_service.dispatch_events(sessions, sender,
                                kinds=["assistant.message.execute", "assistant.card.build", "content_index_build"])
            return {"events_sent": sent}
        finally:
            await engine.dispose()
    return asyncio.run(dispatch())


@celery_app.task(name="content_index_build", max_retries=0)
def content_index_build_task(revision_id: str):
    """Build an immutable assistant index after P11 content publication."""
    import asyncio
    if not settings.app.assistant_enabled:
        return {"status": "disabled"}
    async def build():
        from app.services.assistant.index_service import IndexService
        from app.services.assistant.model_providers import providers_from_settings
        engine = create_async_engine(settings.database.url, poolclass=NullPool)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            providers = providers_from_settings(settings)
            service = IndexService(sessions, embedding_provider=providers.embedding)
            result = await service.build(UUID(revision_id), providers.index_config())
            generation = result.generation
            head = await service.repository.get_head(generation.document_id)
            await service.publish(generation.generation_id, expected_cas_version=head.cas_version,
                expected_revision_id=UUID(revision_id), expected_invalidation_watermark=head.invalidation_watermark)
            return {"status": "eligible", "generation_id": str(generation.generation_id),
                    "revision_id": revision_id, "semantic_status": result.embedding_status}
        finally:
            await engine.dispose()
    return asyncio.run(build())


@celery_app.task(name="assistant_card_build", max_retries=0)
def assistant_card_build_task(card_id: str):
    import asyncio
    if not settings.app.assistant_enabled:
        return {"status": "disabled"}

    async def build():
        from app.services.assistant.card_service import card_service
        engine = create_async_engine(settings.database.url, poolclass=NullPool)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            return await card_service.build(sessions, UUID(card_id))
        finally:
            await engine.dispose()
    return asyncio.run(build())
