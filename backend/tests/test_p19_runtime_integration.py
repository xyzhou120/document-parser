"""P19 运行时集成检查，使用真实 Redis Lua 和非立即执行的 Celery 工作进程。

这些测试必须通过 P05_TEST_DATABASE_NAME 显式启用。它们只使用隔离 G1 数据库中的模拟记录，
从不调用模型服务。Redis 键按测试隔离命名空间，并通过该命名空间清理。
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta
from threading import Event, Lock
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from celery import Celery
from celery.contrib.testing.worker import start_worker
from redis.asyncio import Redis
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings
from app.services.execution import QuantumOutcome
from app.services.reliable_extraction_service import reliable_extraction_service
from app.services.service_circuit import CircuitPolicy, RedisCircuitStore, ServiceCircuit
from tests.test_field_scheduler_postgres import seed


class _ExpiringRedis:
    """原样运行生产 Lua，并限制每个测试键的生命周期。"""

    def __init__(self, redis):
        self.redis = redis

    async def eval(self, script, numkeys, *args):
        result = await self.redis.eval(script, numkeys, *args)
        await self.redis.expire(args[0], 60)
        return result


@pytest_asyncio.fixture
async def sessions():
    database = os.environ.get("P05_TEST_DATABASE_NAME", "")
    if not database:
        pytest.skip("set P05_TEST_DATABASE_NAME to the isolated G1 database")
    if not database.startswith("document_parser_v2_g1_"):
        raise ValueError("runtime tests require an isolated G1 database")
    engine = create_async_engine(make_url(settings.database.url).set(database=database), poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_real_redis_lua_circuit_is_shared_and_epoch_fenced():
    redis: Redis = Redis.from_url(settings.redis.url, decode_responses=False,
                                  socket_connect_timeout=4, socket_timeout=4)
    peer: Redis = Redis.from_url(settings.redis.url, decode_responses=False,
                                socket_connect_timeout=4, socket_timeout=4)
    namespace = f"document-parser:p19:{uuid4().hex}"
    policy = CircuitPolicy(threshold=2, cooldown_seconds=1, half_open_limit=1, max_recoveries=3)
    now = [datetime.utcnow()]
    circuit = ServiceCircuit(RedisCircuitStore(_ExpiringRedis(redis), namespace=namespace), policy,
                             clock=lambda: now[0])
    other_worker = ServiceCircuit(RedisCircuitStore(_ExpiringRedis(peer), namespace=namespace), policy,
                                  clock=lambda: now[0])
    key = "model:test"
    try:
        assert await redis.ping()
        first = await circuit.acquire(key)
        assert first.allowed and not first.probe
        await circuit.failure(key, first)
        second = await circuit.acquire(key)
        assert second.allowed
        await circuit.failure(key, second)
        blocked = await other_worker.acquire(key)
        assert not blocked.allowed and not blocked.exhausted

        now[0] += timedelta(seconds=2)
        probe = await circuit.acquire(key)
        assert probe.allowed and probe.probe
        competing = await other_worker.acquire(key)
        assert not competing.allowed
        await circuit.success(key, probe)
        recovered = await circuit.acquire(key)
        assert recovered.allowed and recovered.epoch > probe.epoch

        # 旧版本的过期报告不能重新打开或关闭熔断器。
        await circuit.failure(key, probe)
        still_open = await circuit.acquire(key)
        assert still_open.allowed
        assert 0 < await redis.ttl(f"{namespace}:{key}") <= 60
    finally:
        await redis.delete(f"{namespace}:{key}")
        await redis.aclose()
        await peer.aclose()


@pytest.mark.asyncio
async def test_non_eager_celery_worker_delivers_outbox_and_runs_multiple_quantums(sessions):
    run_id, _, _ = await seed(sessions, 2)
    calls: dict[str, int] = {}
    calls_lock = Lock()

    async def executor(token, checkpoint):
        with calls_lock:
            count = calls.get(str(token.job_id), 0) + 1
            calls[str(token.job_id)] = count
        if count == 1:
            return QuantumOutcome("yielded", {"cursor": 1})
        return QuantumOutcome("done", {"cursor": 2}, semantic_state="completed")

    # 第一个有界窗口会在发件箱中留下持久化唤醒工作。
    initial = await reliable_extraction_service.process_run(
        sessions, run_id, executor=executor, owner="p19-seed", max_quantums=1
    )
    assert initial["remaining_runnable"] >= 1

    app = Celery(f"p19-runtime-{uuid4().hex}", broker="memory://", backend="cache+memory://")
    queue = f"p19-runtime-{uuid4().hex}"
    app.conf.update(task_default_queue=queue, task_always_eager=False, task_serializer="json",
                    result_serializer="json", accept_content=["json"], worker_prefetch_multiplier=1)
    finished = Event()
    results: list[dict] = []
    delivered: list[dict] = []
    broker_results = []

    async def sender(kind, payload):
        delivered.append({"kind": kind, "payload": payload})
        broker_results.append(continue_task.apply_async(args=[payload["run_id"],
            payload.get("dispatch_event_id"), payload.get("dispatch_attempt")], queue=queue))

    @app.task(name="p19.runtime.continue")
    def continue_task(parse_run_id: str, event_id=None, attempt=None):
        async def run():
            if event_id:
                from app.services.execution import JobRepository
                async with sessions() as db, db.begin():
                    consumed = await JobRepository(db).consume_run_wakeup(
                        UUID(parse_run_id), UUID(event_id), dispatch_attempt=attempt)
                if not consumed:
                    return {"status": "duplicate_or_stale_wakeup"}
            result = await reliable_extraction_service.process_run(
                sessions, UUID(parse_run_id),
                executor=executor, owner="p19-worker", max_quantums=1
            )
            results.append(result)
            await reliable_extraction_service.dispatch_events(sessions, sender, limit=20, run_id=run_id)
            if result["status"] == "COMPLETED":
                finished.set()
            return result
        return asyncio.run(run())

    sent = await reliable_extraction_service.dispatch_events(
        sessions, sender, limit=20, run_id=run_id
    )
    assert sent >= 1
    assert delivered and delivered[0]["payload"]["run_id"] == str(run_id)

    with start_worker(app, perform_ping_check=False, concurrency=1, pool="solo", loglevel="WARNING", queues=[queue]):
        # 工作进程在入队后启动，以证明消息代理确实完成投递；
        # 有界等待可避免工作进程故障时测试挂起。
        await asyncio.wait_for(asyncio.to_thread(finished.wait, 15), timeout=20)
        assert finished.is_set()
        for message in list(broker_results):
            payload = await asyncio.to_thread(message.get, timeout=10)
            assert isinstance(json.loads(json.dumps(payload)), dict)

    result = results[-1]
    assert result["status"] == "COMPLETED"
    assert len(results) >= 3
    assert sum(calls.values()) == 4
    assert result["remaining_runnable"] == 0
    app.close()
