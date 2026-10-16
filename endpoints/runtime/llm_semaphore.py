"""Cross-process FIFO model slots. Waiting consumes the caller's deadline."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass

from redis import asyncio as redis_asyncio
from redis.exceptions import RedisError

from config import MODEL_SEMAPHORE_CONFIGS
from endpoints.runtime.model_budget import ModelCallBudget, ModelCallTimeout, run_sync


class ModelSemaphoreTimeout(ModelCallTimeout):
    pass


LLMGlobalSemaphoreTimeout = ModelSemaphoreTimeout


@dataclass(frozen=True)
class ModelSemaphoreConfig:
    redis_url: str
    key: str
    max_concurrent_requests: int
    lease_seconds: float
    poll_interval_seconds: float


@dataclass
class ModelSemaphoreLease:
    model_type: str
    key: str
    member: str
    wait_seconds: float
    max_concurrent_requests: int
    acquired: bool = True


LLMGlobalSemaphoreLease = ModelSemaphoreLease


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
    # One atomic operation cleans expired owners/waiters, validates the shared
    # capacity policy, enqueues once, and gives the oldest waiter the next slot.
    _ACQUIRE_SCRIPT = """
    redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
    local expired = redis.call('ZRANGEBYSCORE', KEYS[3], '-inf', ARGV[1])
    for _, member in ipairs(expired) do
        redis.call('ZREM', KEYS[2], member)
        redis.call('ZREM', KEYS[3], member)
    end
    local policy = redis.call('GET', KEYS[5])
    if policy and policy ~= ARGV[6] and
       (redis.call('ZCARD', KEYS[1]) > 0 or redis.call('ZCARD', KEYS[2]) > 0) then
        return -1
    end
    redis.call('SET', KEYS[5], ARGV[6], 'EX', ARGV[5])
    if not redis.call('ZSCORE', KEYS[2], ARGV[4]) then
        local sequence = redis.call('INCR', KEYS[4])
        redis.call('ZADD', KEYS[2], sequence, ARGV[4])
        redis.call('ZADD', KEYS[3], ARGV[7], ARGV[4])
    end
    for index = 1, 4 do redis.call('EXPIRE', KEYS[index], ARGV[5]) end
    if redis.call('ZCARD', KEYS[1]) < tonumber(ARGV[3])
       and redis.call('ZRANK', KEYS[2], ARGV[4]) == 0 then
        redis.call('ZREM', KEYS[2], ARGV[4])
        redis.call('ZREM', KEYS[3], ARGV[4])
        redis.call('ZADD', KEYS[1], ARGV[2], ARGV[4])
        return 1
    end
    return 0
    """

    def __init__(self, model_type: str, config: ModelSemaphoreConfig):
        self.model_type = model_type
        self.config = config
        self._clients = {}

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
        keys = [key, key + ':waiting', key + ':deadlines', key + ':sequence', key + ':policy']
        try:
            async with asyncio.timeout(budget.remaining()):
                while True:
                    remaining = budget.remaining()
                    now_ms = int(time.time() * 1000)
                    result = await client.eval(
                        self._ACQUIRE_SCRIPT, len(keys), *keys, now_ms,
                        now_ms + int(self.config.lease_seconds * 1000),
                        self.config.max_concurrent_requests, member,
                        int(self.config.lease_seconds) + 60,
                        f"{self.config.max_concurrent_requests}:{self.config.lease_seconds}",
                        now_ms + int(remaining * 1000),
                    )
                    if result == -1:
                        raise RuntimeError(f"Inconsistent model semaphore policy for {key}; drain old workers and use identical settings.")
                    if result == 1:
                        budget.remaining()
                        return ModelSemaphoreLease(self.model_type, key, member, time.monotonic()-start,
                                                   self.config.max_concurrent_requests)
                    await asyncio.sleep(min(self.config.poll_interval_seconds, budget.remaining()))
        except BaseException as exc:
            # Also remove a slot if cancellation raced with a successful EVAL.
            try:
                await _finish_cleanup(self._remove(member, include_owner=True))
            except (RedisError, TimeoutError):
                logging.getLogger(__name__).exception("Redis slot cleanup failed; lease will reclaim %s", member)
            if isinstance(exc, TimeoutError):
                raise ModelSemaphoreTimeout(f"Model budget expired waiting for {self.model_type} capacity.") from exc
            raise

    async def _remove(self, member: str, *, include_owner: bool):
        client = self._async_client()
        async with asyncio.timeout(2):
            async with client.pipeline(transaction=True) as pipe:
                for suffix in (['', ':waiting', ':deadlines'] if include_owner else [':waiting', ':deadlines']):
                    pipe.zrem(self.config.key + suffix, member)
                await pipe.execute()

    async def release_async(self, lease: ModelSemaphoreLease):
        if lease.acquired:
            await self._remove(lease.member, include_owner=True)
            lease.acquired = False

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
        yield lease
    except (asyncio.CancelledError, TimeoutError):
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
            f"{prefix}_max_requests": lease.max_concurrent_requests}


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
