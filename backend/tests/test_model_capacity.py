import asyncio
import os
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import pytest

from app.services.model_capacity import ModelCapacityUnavailable, RedisModelCapacity, model_identity


class FakeRedis:
    def __init__(self, *, renew_result=1):
        self.renew_result = renew_result
        self.acquired = []
        self.released = []

    async def eval(self, script, _key_count, _key, _limit_key, *args):
        if "ZADD" in script and "XX" not in script:
            token = args[-1]
            self.acquired.append(token)
            return 1
        return self.renew_result

    async def zrem(self, _key, token):
        self.released.append(token)


def test_model_identity_shares_v1_aliases_but_separates_models():
    assert model_identity("HTTP://Example.test:80/v1/", "qwen") == model_identity(
        "http://example.test", "qwen")
    assert model_identity("http://example.test", "qwen") != model_identity(
        "http://example.test", "qwen-vision")


def test_capacity_hold_releases_after_success_and_renew_failure_is_fail_closed():
    async def run():
        redis = FakeRedis()
        capacity = RedisModelCapacity(redis, "http://model.test/v1", "qwen", limit=12)
        token = await capacity.try_acquire()
        async with capacity.hold(token):
            pass
        assert redis.released == [token]

        lost_redis = FakeRedis(renew_result=0)
        lost = RedisModelCapacity(lost_redis, "http://model.test/v1", "qwen", limit=12)
        lost.heartbeat_seconds = 0.005
        lost_token = await lost.try_acquire()
        with pytest.raises(ModelCapacityUnavailable, match="model_capacity_lease_lost"):
            async with lost.hold(lost_token):
                await asyncio.sleep(lost.heartbeat_seconds + 0.01)
        assert lost_redis.released == [lost_token]

    asyncio.run(run())


def test_external_cancel_releases_capacity():
    async def run():
        redis = FakeRedis()
        capacity = RedisModelCapacity(redis, "http://model.test", "qwen", limit=12)
        entered = asyncio.Event()

        async def work():
            token = await capacity.try_acquire()
            async with capacity.hold(token):
                entered.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(work())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert redis.released == redis.acquired

    asyncio.run(run())


class SharedCapacity:
    """仅用于网关行为测试；Lua 的原子性由隔离 Redis 集成测试覆盖。"""
    def __init__(self):
        self.tokens = set()
        self.peak = 0
        self.attempts = 0

    async def try_acquire(self):
        self.attempts += 1
        if len(self.tokens) >= 12:
            return None
        token = uuid4().hex
        self.tokens.add(token)
        self.peak = max(self.peak, len(self.tokens))
        return token

    @asynccontextmanager
    async def hold(self, token):
        try:
            yield
        finally:
            self.tokens.remove(token)


def test_gateway_shared_limit_waits_without_retry_lineage_change_and_replays_when_full():
    from tests.test_model_gateway import gateway, request, response, decision

    async def run():
        shared = SharedCapacity()
        blocked = asyncio.Event()
        entered = asyncio.Event()
        calls = 0

        async def send(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 12:
                entered.set()
            await blocked.wait()
            return response([decision("a")])

        gateways = [gateway([]) for _ in range(36)]
        requests = [request("a") for _ in gateways]
        for gw, client, _ in gateways:
            gw.capacity = shared
            client.create = send
        tasks = [asyncio.create_task(gw.execute(req)) for (gw, _, _), req in zip(gateways, requests)]
        await asyncio.wait_for(entered.wait(), 1)
        while not all(t.done() for t in tasks[12:]):
            await asyncio.sleep(0)
        waits = [t.result() for t in tasks[12:]]
        assert calls == shared.peak == 12
        assert all(w.error == "model_capacity" and w.wait_state == "waiting_service" for w in waits)
        for (_, _, ledger), w in zip(gateways[12:], waits):
            record = await ledger.get(UUID(w.call_id))
            assert record.state == "queued" and record.retry_seq == 0 and record.retry_of_call_id is None
        blocked.set()
        await asyncio.gather(*tasks)
        assert not shared.tokens
        gw, _, ledger = gateways[12]
        done = await gw.execute(requests[12])
        assert done.state == "succeeded" and done.call_id == waits[0].call_id
        assert (await ledger.get(UUID(done.call_id))).retry_seq == 0
        shared.tokens = {str(i) for i in range(12)}
        attempts = shared.attempts
        replay = await gw.execute(requests[12])
        assert replay.replayed and replay.call_id == done.call_id
        assert shared.attempts == attempts and calls == 13

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["timeout", "claim"])
def test_gateway_failure_releases_capacity(failure):
    from unittest.mock import AsyncMock
    from tests.test_model_gateway import gateway, request

    async def run():
        gw, _, ledger = gateway([TimeoutError()])
        shared = gw.capacity = SharedCapacity()
        if failure == "claim":
            ledger.claim = AsyncMock(side_effect=RuntimeError("claim failed"))
            with pytest.raises(RuntimeError, match="claim failed"):
                await gw.execute(request("a"))
        else:
            assert (await gw.execute(request("a"))).state == "timeout"
        assert not shared.tokens

    asyncio.run(run())


@pytest.mark.skipif(os.getenv("MODEL_CAPACITY_REDIS_TEST") != "1", reason="requires isolated Redis keys")
def test_real_redis_atomic_limit_renew_expiry_and_mixed_configs():
    from redis.asyncio import Redis
    from app.config import settings

    async def run():
        redis = Redis.from_url(settings.redis.url, socket_timeout=5, socket_connect_timeout=5)
        namespace = "document-parser:test-model-capacity:" + uuid4().hex
        capacities = [RedisModelCapacity(redis, "http://model.test/v1", "qwen", limit=12,
                                        namespace=namespace) for _ in range(60)]
        first = capacities[0]
        try:
            tokens = await asyncio.gather(*(c.try_acquire() for c in capacities))
            assert sum(t is not None for t in tokens) == 12
            assert await redis.zcard(first.key) == 12
            token = next(t for t in tokens if t)
            await redis.expire(first.limit_key, 1)
            assert await first.renew(token)
            assert await redis.ttl(first.limit_key) > 300
            await first.release(token)
            assert await first.try_acquire()
            small = RedisModelCapacity(redis, "http://MODEL.test:80", "qwen", limit=4, namespace=namespace)
            assert await small.try_acquire() is None
            assert await redis.get(first.limit_key) == b"4"
            await redis.zremrangebyrank(first.key, 0, -5)  # leave four live slots
            assert await first.try_acquire() is None
            # 使用 Redis 时钟构造过期租约，不等待实际 300 秒。
            seconds, micros = await redis.time()
            now = seconds + micros / 1e6
            for t in await redis.zrange(first.key, 0, -1):
                await redis.zadd(first.key, {t: now - 1})
            assert not await first.renew(token)
            assert await first.try_acquire()
            assert await redis.get(first.limit_key) == b"12"
            other = RedisModelCapacity(redis, "http://model.test/v1", "other", limit=12, namespace=namespace)
            try:
                assert await other.try_acquire()
            finally:
                await redis.delete(other.key, other.limit_key)
        finally:
            await redis.delete(first.key, first.limit_key)
            await redis.aclose()

    asyncio.run(run())

