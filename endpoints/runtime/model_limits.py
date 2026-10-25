"""Atomic, cross-process FIFO admission for concurrency and rolling-minute RPM."""
from __future__ import annotations

import asyncio
import logging
import math
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass

from redis import asyncio as redis_asyncio
from redis.exceptions import RedisError

from config import MODEL_SEMAPHORE_CONFIGS
from endpoints.runtime.model_budget import ModelCallBudget, ModelCallTimeout, ModelSemaphoreTimeout, run_sync


LLMGlobalSemaphoreTimeout = ModelSemaphoreTimeout


@dataclass(frozen=True)
class ModelSemaphoreConfig:
    redis_url: str
    key: str
    max_concurrent_requests: int | None
    lease_seconds: float
    poll_interval_seconds: float
    max_requests_per_minute: int | None = None


@dataclass
class ModelSemaphoreLease:
    model_type: str
    key: str
    member: str
    wait_seconds: float
    max_concurrent_requests: int | None
    acquired: bool = True
    renewals: int = 0
    sent: bool = False
    max_requests_per_minute: int | None = None
    concurrency_wait_seconds: float = 0.0
    rpm_wait_seconds: float = 0.0
    window_count: int = 0
    admitted_at_ms: int | None = None
    acquired_concurrency: int = 0


LLMGlobalSemaphoreLease = ModelSemaphoreLease


class ModelLeaseLost(RuntimeError):
    """The request must stop because its shared slot cannot be renewed."""

    failure_stage = "semaphore_renewal"


async def _finish_cleanup(coroutine):
    """Finish one bounded cleanup before propagating external cancellation.

    Shielding once lets the caller leave while cleanup is still running. Keep
    waiting for the same task if cancellation arrives during cleanup, including
    repeated cancellation. The cleanup operation owns its existing two-second
    deadline; cancellation never creates a new task or restarts that deadline.
    """
    cleanup = asyncio.create_task(coroutine)
    cancellation = None
    while True:
        try:
            result = await asyncio.shield(cleanup)
        except asyncio.CancelledError as exc:
            if cleanup.cancelled():
                raise
            cancellation = exc
            continue
        except Exception:
            if cancellation is not None:
                logging.getLogger(__name__).exception("Redis cleanup failed during cancellation; lease will reclaim its slot")
                raise cancellation from None
            raise
        if cancellation is not None:
            raise cancellation
        return result


class RedisModelSemaphore:
    # Redis time is authoritative. Waiting heartbeats are finite even if the
    # business wait has no limit, so a crashed FIFO head cannot block followers.
    _ACQUIRE_SCRIPT = """
    local clock = redis.call('TIME')
    local now = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
    redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
    redis.call('ZREMRANGEBYSCORE', KEYS[6], '-inf', now - 60000)
    local expired = redis.call('ZRANGEBYSCORE', KEYS[3], '-inf', now)
    for _, member in ipairs(expired) do
        redis.call('ZREM', KEYS[2], member)
        redis.call('ZREM', KEYS[3], member)
    end
    local policy = redis.call('GET', KEYS[5])
    if policy and policy ~= ARGV[6] and
       (redis.call('ZCARD', KEYS[1]) > 0 or redis.call('ZCARD', KEYS[2]) > 0
        or redis.call('ZCARD', KEYS[6]) > 0) then
        return {-1, 0, 0, 0}
    end
    redis.call('SET', KEYS[5], ARGV[6], 'EX', ARGV[5])
    if not redis.call('ZSCORE', KEYS[2], ARGV[4]) then
        local sequence = redis.call('INCR', KEYS[4])
        redis.call('ZADD', KEYS[2], sequence, ARGV[4])
    end
    redis.call('ZADD', KEYS[3], now + tonumber(ARGV[7]), ARGV[4])
    for index = 1, 6 do redis.call('EXPIRE', KEYS[index], ARGV[5]) end
    local active = redis.call('ZCARD', KEYS[1])
    local requests = redis.call('ZCARD', KEYS[6])
    local concurrency_blocked = tonumber(ARGV[3]) > 0 and active >= tonumber(ARGV[3])
    local rpm_blocked = tonumber(ARGV[8]) > 0 and requests >= tonumber(ARGV[8])
    if not concurrency_blocked and not rpm_blocked
       and redis.call('ZRANK', KEYS[2], ARGV[4]) == 0 then
        redis.call('ZREM', KEYS[2], ARGV[4])
        redis.call('ZREM', KEYS[3], ARGV[4])
        redis.call('ZADD', KEYS[1], now + tonumber(ARGV[2]), ARGV[4])
        redis.call('ZADD', KEYS[6], now, ARGV[4])
        return {1, 0, active + 1, requests + 1, now}
    end
    local reason = 3
    if concurrency_blocked then reason = 1 elseif rpm_blocked then reason = 2 end
    return {0, reason, active, requests}
    """

    _RENEW_SCRIPT = """
    local clock = redis.call('TIME')
    local now = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
    local score = redis.call('ZSCORE', KEYS[1], ARGV[1])
    if not score or tonumber(score) <= now then
        return 0
    end
    if redis.call('GET', KEYS[5]) ~= ARGV[4] then
        return 0
    end
    redis.call('ZADD', KEYS[1], 'XX', now + tonumber(ARGV[2]), ARGV[1])
    for index = 1, 6 do redis.call('EXPIRE', KEYS[index], ARGV[3]) end
    return 1
    """

    _REMOVE_SCRIPT = """
    redis.call('ZREM', KEYS[2], ARGV[1])
    redis.call('ZREM', KEYS[3], ARGV[1])
    if ARGV[2] == '1' then redis.call('ZREM', KEYS[1], ARGV[1]) end
    if ARGV[3] == '1' then redis.call('ZREM', KEYS[6], ARGV[1]) end
    return 1
    """

    def __init__(self, model_type: str, config: ModelSemaphoreConfig):
        self.model_type = model_type
        self.config = config
        self._clients = {}

    def _keys(self):
        key = self.config.key
        return [key, key + ':waiting', key + ':deadlines', key + ':sequence',
                key + ':policy', key + ':rpm']

    @property
    def _policy(self):
        return f"{self.config.max_concurrent_requests}:{self.config.max_requests_per_minute}:{self.config.lease_seconds}"

    @property
    def _ttl_seconds(self):
        # RPM events must survive at least the whole sliding window, including
        # short test leases and temporarily idle service instances.
        return max(120, math.ceil(self.config.lease_seconds) + 60)

    def _async_client(self):
        loop = asyncio.get_running_loop()
        if loop not in self._clients:
            self._clients[loop] = redis_asyncio.from_url(
                self.config.redis_url, decode_responses=True,
                socket_connect_timeout=2, socket_timeout=2,
            )
        return self._clients[loop]

    async def acquire_async(self, *, caller_name: str, model: str, budget: ModelCallBudget) -> ModelSemaphoreLease:
        start = time.monotonic()
        member = f"{uuid.uuid4().hex}:{self.model_type}:{caller_name}:{model}"
        client = self._async_client()
        key = self.config.key
        keys = self._keys()
        wait_by_reason = {1: 0.0, 2: 0.0, 3: 0.0}
        reason = 3
        last_checked = start
        waiter_heartbeat_seconds = max(2.0, min(self.config.lease_seconds, 30.0))
        try:
            with budget.waiting():
                try:
                    async with asyncio.timeout(budget.waiting_remaining()):
                        while True:
                            budget.waiting_remaining()
                            checked = time.monotonic()
                            wait_by_reason[reason] += checked - last_checked
                            last_checked = checked
                            result = await client.eval(
                                self._ACQUIRE_SCRIPT, len(keys), *keys, 0,
                                int(self.config.lease_seconds * 1000),
                                self.config.max_concurrent_requests or 0, member,
                                self._ttl_seconds, self._policy,
                                int(waiter_heartbeat_seconds * 1000),
                                self.config.max_requests_per_minute or 0,
                            )
                            values = list(result) if isinstance(result, (list, tuple)) else [result, 3, 0, 0]
                            admitted, reason, active, window_count = values[:4]
                            if admitted == -1:
                                raise RuntimeError(f"Inconsistent model semaphore policy for {key}; drain old workers and use identical settings.")
                            if admitted == 1:
                                budget.waiting_remaining()
                                return ModelSemaphoreLease(self.model_type, key, member, time.monotonic()-start,
                                    self.config.max_concurrent_requests,
                                    max_requests_per_minute=self.config.max_requests_per_minute,
                                    concurrency_wait_seconds=wait_by_reason[1], rpm_wait_seconds=wait_by_reason[2],
                                    window_count=window_count,
                                    admitted_at_ms=values[4] if len(values) > 4 else None,
                                    acquired_concurrency=active)
                            await asyncio.sleep(min(self.config.poll_interval_seconds,
                                                    waiter_heartbeat_seconds / 3, budget.waiting_remaining()))
                except TimeoutError as exc:
                    if isinstance(exc, ModelCallTimeout):
                        raise
                    # Distinguish task expiry from an acquisition timer firing.
                    budget.waiting_remaining()
                    raise ModelSemaphoreTimeout(f"Slot-wait budget expired for {self.model_type} capacity.") from exc
        except BaseException:
            # Also remove a slot if cancellation raced with a successful EVAL.
            try:
                await _finish_cleanup(self._remove(member, include_owner=True))
            except (RedisError, TimeoutError):
                logging.getLogger(__name__).exception("Redis slot cleanup failed; lease will reclaim %s", member)
            raise

    async def _remove(self, member: str, *, include_owner: bool, remove_rpm: bool = True):
        client = self._async_client()
        async with asyncio.timeout(2):
            await client.eval(self._REMOVE_SCRIPT, 6, *self._keys(), member,
                              int(include_owner), int(remove_rpm))

    async def release_async(self, lease: ModelSemaphoreLease):
        if lease.acquired:
            await self._remove(lease.member, include_owner=True, remove_rpm=not lease.sent)
            lease.acquired = False

    async def keep_alive(self, lease: ModelSemaphoreLease):
        """Renew this owner every third of a lease; never recreate a lost slot."""
        while True:
            await asyncio.sleep(self.config.lease_seconds / 3)
            key = self.config.key
            try:
                async with asyncio.timeout(min(2, self.config.lease_seconds / 3)):
                    renewed = await self._async_client().eval(
                        self._RENEW_SCRIPT, 6, *self._keys(), lease.member,
                        int(self.config.lease_seconds * 1000),
                        self._ttl_seconds, self._policy,
                    )
            except (RedisError, TimeoutError) as exc:
                raise ModelLeaseLost(f"Cannot renew {self.model_type} model slot: {exc}") from exc
            if renewed != 1:
                raise ModelLeaseLost(f"The {self.model_type} model slot expired, was removed, or its policy changed.")
            lease.renewals += 1

    def acquire_sync(self, *, caller_name: str, model: str, budget: ModelCallBudget):
        return run_sync(self.acquire_async(caller_name=caller_name, model=model, budget=budget))

    def release_sync(self, lease: ModelSemaphoreLease):
        return run_sync(self.release_async(lease))


_SEMAPHORES = {kind: RedisModelSemaphore(kind, ModelSemaphoreConfig(**raw))
               for kind, raw in MODEL_SEMAPHORE_CONFIGS.items()}


@asynccontextmanager
async def model_semaphore(model_type: str, caller_name: str, model: str, *, budget: ModelCallBudget | None = None):
    semaphore = _SEMAPHORES[model_type]
    lease = await semaphore.acquire_async(caller_name=caller_name, model=model, budget=budget or ModelCallBudget())
    interrupted = False
    try:
        try:
            async with asyncio.TaskGroup() as group:
                renewal = group.create_task(semaphore.keep_alive(lease))
                try:
                    yield lease
                finally:
                    renewal.cancel()
        except BaseExceptionGroup as exc:
            # TaskGroup stops the HTTP coroutine if renewal fails. Preserve
            # ordinary request/timeout exception types for the shared retry
            # controller instead of wrapping them in a one-element group.
            if len(exc.exceptions) == 1:
                raise exc.exceptions[0] from None
            raise
    except (asyncio.CancelledError, TimeoutError, ModelLeaseLost):
        interrupted = True
        raise
    finally:
        try:
            await _finish_cleanup(semaphore.release_async(lease))
        except Exception:
            if not interrupted:
                raise
            logging.getLogger(__name__).exception("Redis cleanup failed after interruption; lease will reclaim its slot")


@contextmanager
def model_semaphore_sync(model_type: str, caller_name: str, model: str, *, budget: ModelCallBudget | None = None):
    semaphore = _SEMAPHORES[model_type]
    lease = semaphore.acquire_sync(caller_name=caller_name, model=model, budget=budget or ModelCallBudget())
    try:
        yield lease
    finally:
        semaphore.release_sync(lease)


def semaphore_metadata(lease: ModelSemaphoreLease) -> dict:
    prefix = f"{lease.model_type}_global_semaphore"
    return {f"{prefix}_key": lease.key, f"{prefix}_wait_seconds": lease.wait_seconds,
            f"{prefix}_max_requests": lease.max_concurrent_requests,
            "max_requests_per_minute": lease.max_requests_per_minute,
            "rpm_window_count": lease.window_count,
            "admitted_at_ms": lease.admitted_at_ms,
            "admitted_at": lease.admitted_at_ms / 1000 if lease.admitted_at_ms is not None else None,
            "acquired_concurrency": lease.acquired_concurrency,
            "concurrency_wait_seconds": lease.concurrency_wait_seconds,
            "rpm_wait_seconds": lease.rpm_wait_seconds}


@asynccontextmanager
async def llm_global_semaphore(agent_name: str, model: str, *, budget: ModelCallBudget | None = None):
    async with model_semaphore('llm', agent_name, model, budget=budget) as lease:
        yield lease


async def close_model_clients() -> None:
    loop = asyncio.get_running_loop()
    for semaphore in _SEMAPHORES.values():
        client = semaphore._clients.pop(loop, None)
        if client is not None:
            await client.aclose()
