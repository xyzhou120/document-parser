"""同一模型部署跨 worker 共用的请求名额，与历史调用账本独立。"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from contextlib import asynccontextmanager, suppress
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

logger = logging.getLogger(__name__)

_ACQUIRE = """
local clock = redis.call('TIME')
local now = tonumber(clock[1]) + tonumber(clock[2]) / 1000000
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
local count = redis.call('ZCARD', KEYS[1])
local limit = tonumber(ARGV[1])
if count > 0 then limit = math.min(limit, tonumber(redis.call('GET', KEYS[2]) or limit)) end
redis.call('SET', KEYS[2], limit, 'EX', math.ceil(tonumber(ARGV[2]) + 60))
if count >= limit then return 0 end
redis.call('ZADD', KEYS[1], now + tonumber(ARGV[2]), ARGV[3])
redis.call('EXPIRE', KEYS[1], math.ceil(tonumber(ARGV[2]) + 60))
redis.call('EXPIRE', KEYS[2], math.ceil(tonumber(ARGV[2]) + 60))
return 1
"""
_RENEW = """
local clock = redis.call('TIME')
local now = tonumber(clock[1]) + tonumber(clock[2]) / 1000000
local expiry = redis.call('ZSCORE', KEYS[1], ARGV[1])
if not expiry or tonumber(expiry) <= now then return 0 end
redis.call('ZADD', KEYS[1], 'XX', now + tonumber(ARGV[2]), ARGV[1])
redis.call('EXPIRE', KEYS[1], math.ceil(tonumber(ARGV[2]) + 60))
redis.call('EXPIRE', KEYS[2], math.ceil(tonumber(ARGV[2]) + 60))
return 1
"""


def model_identity(base_url: str, model: str) -> str:
    base = str(base_url).strip().rstrip('/')
    if '://' not in base:
        base = 'http://' + base
    parts = urlsplit(base)
    path = parts.path.rstrip('/')
    if path.endswith('/v1'):
        path = path[:-3]
    # 服务地址、模型相同即共池；版本、API key 和 text/vision 配置名不分池。
    host = (parts.hostname or '').lower()
    host = '[' + host + ']' if ':' in host else host
    port = parts.port
    if port and (parts.scheme.lower(), port) not in {('http', 80), ('https', 443)}:
        host += ':' + str(port)
    normalized = urlunsplit((parts.scheme.lower(), host, path, '', ''))
    return hashlib.sha256((normalized + '\n' + str(model)).encode()).hexdigest()


def configured_limit(base_url: str, model: str) -> int:
    from app.config import settings
    identity = model_identity(base_url, model)
    limits = [config.max_concurrency for key, config in settings.models.items()
              if key in {'text_llm', 'vision_llm'} and model_identity(config.base_url, config.model_name) == identity]
    return min(limits) if limits else 12


class ModelCapacityUnavailable(ConnectionError):
    pass


class RedisModelCapacity:
    def __init__(self, redis, base_url: str, model: str, *, limit: int | None = None,
                 timeout: float = 180, namespace: str = 'document-parser:model-inflight'):
        self.redis = redis
        self.key = namespace + ':' + model_identity(base_url, model)
        self.limit_key = self.key + ':limit'
        self.limit = configured_limit(base_url, model) if limit is None else limit
        if self.limit <= 0 or timeout <= 0:
            raise ValueError('model capacity and timeout must be positive')
        self.lease_seconds = max(300, timeout + 60)
        self.heartbeat_seconds = min(30, self.lease_seconds / 3)

    async def try_acquire(self) -> str | None:
        token = uuid4().hex
        result = await asyncio.wait_for(self.redis.eval(_ACQUIRE, 2, self.key, self.limit_key,
                                       self.limit, self.lease_seconds, token), timeout=5)
        return token if int(result) == 1 else None

    async def renew(self, token: str) -> bool:
        return bool(await asyncio.wait_for(self.redis.eval(_RENEW, 2, self.key, self.limit_key,
                                                          token, self.lease_seconds), timeout=5))

    async def release(self, token: str) -> None:
        await asyncio.wait_for(self.redis.zrem(self.key, token), timeout=5)

    @asynccontextmanager
    async def hold(self, token: str):
        owner = asyncio.current_task()
        lease_lost = False

        async def heartbeat():
            nonlocal lease_lost
            try:
                while True:
                    await asyncio.sleep(self.heartbeat_seconds)
                    if not await self.renew(token):
                        raise ModelCapacityUnavailable('model_capacity_lease_lost')
            except asyncio.CancelledError:
                raise
            except Exception:
                lease_lost = True
                owner.cancel()

        pulse = asyncio.create_task(heartbeat())
        try:
            yield
        except asyncio.CancelledError:
            if lease_lost:
                raise ModelCapacityUnavailable('model_capacity_lease_lost') from None
            raise
        finally:
            pulse.cancel()
            with suppress(asyncio.CancelledError):
                await pulse
            try:
                await self.release(token)
            except Exception:
                # 不覆盖已付费成功响应；进程退出或 Redis 故障后由租约回收。
                logger.warning('模型请求名额释放失败，等待租约回收', exc_info=True)


@asynccontextmanager
async def model_request_slot(base_url: str, model: str, *, timeout: float = 180):
    """助手、视觉兜底和旧入口共池；这些非账本入口异步等待名额。"""
    from redis.asyncio import Redis
    from app.config import settings
    redis = Redis.from_url(settings.redis.url, socket_connect_timeout=5, socket_timeout=5)
    try:
        capacity = RedisModelCapacity(redis, base_url, model, timeout=timeout)
        deadline = time.monotonic() + timeout
        token = await capacity.try_acquire()
        while token is None:
            if time.monotonic() >= deadline:
                raise ModelCapacityUnavailable('model_capacity_wait_timeout')
            await asyncio.sleep(0.5)
            token = await capacity.try_acquire()
        async with capacity.hold(token):
            yield
    finally:
        await redis.aclose()


@asynccontextmanager
async def limited_model_request(client, model: str, *, timeout: float = 180):
    """当前非流式 SDK 调用统一占名额；注入的测试/替代客户端自行管理容量。"""
    from openai import AsyncOpenAI
    if not isinstance(AsyncOpenAI, type) or not isinstance(client, AsyncOpenAI):
        yield
        return
    async with model_request_slot(str(client.base_url), model, timeout=timeout):
        yield
