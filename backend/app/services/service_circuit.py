"""供 OCR 和模型调用共享的非阻塞服务熔断器。

生产环境使用 Redis 原子状态转换。独立的服务类型和部署配置摘要
可以防止 OCR 故障影响健康的模型服务。
"""

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta


def service_key(kind: str, base_url: str, model: str, config_version: str) -> str:
    if kind not in {"model", "ocr"}:
        raise ValueError("service kind must be model or ocr")
    identity = json.dumps([base_url.rstrip("/"), model, config_version], separators=(",", ":"))
    return f"{kind}:{hashlib.sha256(identity.encode()).hexdigest()}"


@dataclass(frozen=True)
class CircuitPolicy:
    threshold: int = 3
    cooldown_seconds: float = 30
    half_open_limit: int = 1
    max_recoveries: int = 8

    def __post_init__(self):
        if min(self.threshold, self.cooldown_seconds, self.half_open_limit, self.max_recoveries) <= 0:
            raise ValueError("circuit limits must be positive")


@dataclass(frozen=True)
class CircuitPermit:
    allowed: bool
    epoch: int = 0
    probe: bool = False
    retry_at: datetime | None = None
    exhausted: bool = False


def transition(state, operation, now, policy, epoch=None, probe=False):
    if isinstance(now, datetime):
        now = (now - datetime(1970, 1, 1)).total_seconds()
    state = dict(state or {"failures": 0, "until": 0, "probes": 0, "epoch": 0, "recoveries": 0})
    result = {"allowed": False, "epoch": state["epoch"], "probe": False,
              "retry_at": state["until"], "exhausted": False}
    if operation == "acquire":
        if state["recoveries"] >= policy.max_recoveries:
            result["exhausted"] = True
        elif state["until"] == 0:
            result["allowed"] = True
        elif now >= state["until"]:
    # 丢失的探测租约不能让服务一直保持开放。
            if state["probes"]:
                state["probes"] = 0
                state["epoch"] += 1
                state["recoveries"] += 1
            if state["recoveries"] >= policy.max_recoveries:
                result["exhausted"] = True
            elif state["probes"] < policy.half_open_limit:
                state["probes"] += 1
                state["until"] = now + policy.cooldown_seconds
                result.update(allowed=True, probe=True)
        result.update(epoch=state["epoch"], retry_at=state["until"])
    elif epoch == state["epoch"]:
        if operation == "success" and (probe or state["until"] == 0):
            state.update(failures=0, until=0, probes=0, recoveries=0,
                         epoch=state["epoch"] + (1 if probe else 0))
        elif operation == "failure":
            state["failures"] += 1
            if probe or state["failures"] >= policy.threshold:
                state.update(until=now + policy.cooldown_seconds, probes=0,
                             epoch=state["epoch"] + 1, recoveries=state["recoveries"] + 1)
        elif operation == "release" and probe:
            state["probes"] = max(0, state["probes"] - 1)
    return state, result


class MemoryCircuitStore:
    """用于测试的共享存储；工作进程中注入 RedisCircuitStore。"""

    def __init__(self):
        self.states, self.lock = {}, asyncio.Lock()

    async def apply(self, key, operation, now, policy, epoch=None, probe=False):
        async with self.lock:
            state, result = transition(self.states.get(key), operation, now, policy, epoch, probe)
            self.states[key] = state
            return result


_TRANSITION_LUA = """
local s = cjson.decode(redis.call('GET',KEYS[1]) or '{"failures":0,"until":0,"probes":0,"epoch":0,"recoveries":0}')
local op,now,threshold,cooldown,limit,max_recoveries,epoch,probe = ARGV[1],tonumber(ARGV[2]),tonumber(ARGV[3]),tonumber(ARGV[4]),tonumber(ARGV[5]),tonumber(ARGV[6]),tonumber(ARGV[7]),ARGV[8]=='1'
local r = {allowed=false,epoch=s.epoch,probe=false,retry_at=s['until'],exhausted=false}
if op == 'acquire' then
  if s.recoveries >= max_recoveries then r.exhausted=true
  elseif s['until']==0 then r.allowed=true
  elseif now >= s['until'] then
    if s.probes > 0 then s.probes=0;s.epoch=s.epoch+1;s.recoveries=s.recoveries+1 end
    if s.recoveries >= max_recoveries then r.exhausted=true
    elseif s.probes < limit then s.probes=s.probes+1;s['until']=now+cooldown;r.allowed=true;r.probe=true end
  end
  r.epoch=s.epoch;r.retry_at=s['until']
elseif epoch == s.epoch then
  if op=='success' and (probe or s['until']==0) then
    s.failures=0;s['until']=0;s.probes=0;s.recoveries=0;if probe then s.epoch=s.epoch+1 end
  elseif op=='failure' then
    s.failures=s.failures+1
    if probe or s.failures>=threshold then s['until']=now+cooldown;s.probes=0;s.epoch=s.epoch+1;s.recoveries=s.recoveries+1 end
  elseif op=='release' and probe then s.probes=math.max(0,s.probes-1) end
end
redis.call('SET',KEYS[1],cjson.encode(s))
return cjson.encode(r)
"""


class RedisCircuitStore:
    def __init__(self, redis, *, namespace="document-parser:v2:circuit"):
        self.redis, self.namespace = redis, namespace

    async def apply(self, key, operation, now, policy, epoch=None, probe=False):
        result = await self.redis.eval(_TRANSITION_LUA, 1, f"{self.namespace}:{key}",
                                       operation, now, policy.threshold, policy.cooldown_seconds,
                                       policy.half_open_limit, policy.max_recoveries,
                                       -1 if epoch is None else epoch, "1" if probe else "0")
        return json.loads(result)


class ServiceCircuit:
    def __init__(self, store, policy=None, *, clock=datetime.utcnow):
        self.store, self.policy, self.clock = store, policy or CircuitPolicy(), clock

    async def acquire(self, key) -> CircuitPermit:
        now = self.clock()
        result = await self.store.apply(key, "acquire", _timestamp(now), self.policy)
        retry = result["retry_at"]
        return CircuitPermit(result["allowed"], result["epoch"], result["probe"],
                             datetime(1970, 1, 1) + timedelta(seconds=retry) if retry else None,
                             result["exhausted"])

    async def success(self, key, permit):
        await self._report(key, "success", permit)

    async def failure(self, key, permit):
        await self._report(key, "failure", permit)

    async def release(self, key, permit):
        await self._report(key, "release", permit)

    async def _report(self, key, operation, permit):
        await self.store.apply(key, operation, _timestamp(self.clock()), self.policy,
                               permit.epoch, permit.probe)


def _timestamp(value):
    return (value - datetime(1970, 1, 1)).total_seconds()
