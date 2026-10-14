"""全平台共享的 OCR 并发名额。

名额用完时调用方排队，不把这次内容准备记成失败。租约到期后名额自动交还，
避免 worker 中途退出把名额占死。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from uuid import uuid4


_ACQUIRE = """
local key, now, lease, limit, token = KEYS[1], tonumber(ARGV[1]), tonumber(ARGV[2]), tonumber(ARGV[3]), ARGV[4]
redis.call('ZREMRANGEBYSCORE', key, '-inf', now)
if redis.call('ZCARD', key) >= limit then return 0 end
redis.call('ZADD', key, now + lease, token)
redis.call('PEXPIRE', key, math.floor((lease + 30) * 1000))
return 1
"""


class MemoryOCRCapacity:
    """测试用的进程内名额。"""

    def __init__(self):
        self.tokens: dict[str, float] = {}
        self.lock = asyncio.Lock()

    async def try_acquire(self, limit: int, lease_seconds: int, now: float | None = None) -> str | None:
        current = now if now is not None else datetime.utcnow().timestamp()
        async with self.lock:
            self.tokens = {token: expiry for token, expiry in self.tokens.items() if expiry > current}
            if len(self.tokens) >= limit:
                return None
            token = uuid4().hex
            self.tokens[token] = current + lease_seconds
            return token

    async def release(self, token: str | None) -> None:
        if token:
            self.tokens.pop(token, None)


class RedisOCRCapacity:
    def __init__(self, redis, *, namespace: str = "document-parser:v2:ocr-inflight"):
        self.redis = redis
        self.key = namespace

    async def try_acquire(self, limit: int, lease_seconds: int, now: float | None = None) -> str | None:
        current = now if now is not None else datetime.utcnow().timestamp()
        token = uuid4().hex
        acquired = await self.redis.eval(_ACQUIRE, 1, self.key, current, lease_seconds, limit, token)
        return token if int(acquired) == 1 else None

    async def release(self, token: str | None) -> None:
        if token:
            await self.redis.zrem(self.key, token)
