"""Provider quotas, singleflight, and keyed serialization primitives."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Hashable, Mapping
from dataclasses import dataclass
from typing import Generic, TypeVar

from .observability import log_event

T = TypeVar("T")
K = TypeVar("K", bound=Hashable)


class CapacityLease:
    def __init__(self, gate: "CapacityGate", *, acquired: bool = False) -> None:
        self._gate = gate
        self._acquired = acquired
        self._released = False

    async def __aenter__(self) -> "CapacityLease":
        if not self._acquired:
            await self._gate._acquire()
            self._acquired = True
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.release()

    async def release(self) -> None:
        if not self._acquired or self._released:
            return
        self._released = True
        await self._gate._release()


class CapacityGate:
    def __init__(
        self,
        limit: int,
        *,
        provider: str | None = None,
        quota_kind: str | None = None,
        logger: logging.Logger | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if limit <= 0:
            raise ValueError("capacity limit must be positive")
        self.limit = limit
        self.in_use = 0
        self.max_observed_in_use = 0
        self._provider = provider
        self._quota_kind = quota_kind
        self._logger = logger
        self._monotonic = monotonic
        self._condition = asyncio.Condition()

    def lease(self) -> CapacityLease:
        return CapacityLease(self)

    async def try_lease(self) -> CapacityLease | None:
        async with self._condition:
            if self.in_use >= self.limit:
                self._log("quota_waiting")
                return None
            self._claim(waited_ms=0)
            return CapacityLease(self, acquired=True)

    async def wait_until_available(self) -> None:
        async with self._condition:
            if self.in_use >= self.limit:
                self._log("quota_waiting")
            await self._condition.wait_for(lambda: self.in_use < self.limit)

    async def _acquire(self) -> None:
        async with self._condition:
            started = self._monotonic()
            waiting = self.in_use >= self.limit
            if waiting:
                self._log("quota_waiting")
            await self._condition.wait_for(lambda: self.in_use < self.limit)
            waited_ms = max(0, int((self._monotonic() - started) * 1000)) if waiting else 0
            self._claim(waited_ms=waited_ms)

    def _claim(self, *, waited_ms: int) -> None:
        self.in_use += 1
        self.max_observed_in_use = max(self.max_observed_in_use, self.in_use)
        self._log("quota_acquired", waited_ms=waited_ms)

    async def _release(self) -> None:
        async with self._condition:
            if self.in_use <= 0:
                raise RuntimeError("capacity lease released more than once")
            self.in_use -= 1
            self._log("quota_released")
            self._condition.notify_all()

    def _log(self, event: str, *, waited_ms: int | None = None) -> None:
        if self._logger is None or self._provider is None or self._quota_kind is None:
            return
        if waited_ms is None:
            log_event(
                self._logger,
                logging.DEBUG,
                event,
                provider=self._provider,
                quota_kind=self._quota_kind,
                in_use=self.in_use,
                limit=self.limit,
            )
            return
        log_event(
            self._logger,
            logging.DEBUG,
            event,
            provider=self._provider,
            quota_kind=self._quota_kind,
            in_use=self.in_use,
            limit=self.limit,
            waited_ms=waited_ms,
        )


class ProviderQuotaManager:
    def __init__(
        self,
        *,
        web_limits: Mapping[str, int],
        llm_limits: Mapping[str, int],
        academic_limits: Mapping[str, int] | None = None,
        agent_limits: Mapping[str, int] | None = None,
        logger: logging.Logger | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        event_logger = logger or logging.getLogger(__name__)
        self._web = {
            name: CapacityGate(
                limit,
                provider=name,
                quota_kind="web",
                logger=event_logger,
                monotonic=monotonic,
            )
            for name, limit in web_limits.items()
        }
        self._llm = {
            name: CapacityGate(
                limit,
                provider=name,
                quota_kind="llm",
                logger=event_logger,
                monotonic=monotonic,
            )
            for name, limit in llm_limits.items()
        }
        self._academic = {
            name: CapacityGate(
                limit,
                provider=name,
                quota_kind="academic",
                logger=event_logger,
                monotonic=monotonic,
            )
            for name, limit in (academic_limits or {}).items()
        }

        self._agent = {
            name: CapacityGate(
                limit,
                provider=name,
                quota_kind="agent",
                logger=event_logger,
                monotonic=monotonic,
            )
            for name, limit in (agent_limits or {}).items()
        }

    def get_agent(self, name: str) -> CapacityGate:
        return self._agent[name]

    def get_web(self, name: str) -> CapacityGate:
        return self._web[name]

    def get_llm(self, name: str) -> CapacityGate:
        return self._llm[name]

    def get_academic(self, name: str) -> CapacityGate:
        return self._academic[name]

    async def wait_until_any_web_available(self, candidate_names: tuple[str, ...]) -> None:
        if not candidate_names:
            return
        tasks = [
            asyncio.create_task(self.get_web(name).wait_until_available())
            for name in candidate_names
        ]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()


@dataclass(slots=True)
class _LockEntry:
    lock: asyncio.Lock
    references: int = 0


class _KeyedLockLease(Generic[K]):
    def __init__(self, pool: "PerKeyLockPool[K]", key: K) -> None:
        self._pool = pool
        self._key = key
        self._entry: _LockEntry | None = None

    async def __aenter__(self) -> None:
        self._entry = await self._pool._reserve(self._key)
        try:
            await self._entry.lock.acquire()
        except BaseException:
            await self._pool._unreserve(self._key, self._entry)
            self._entry = None
            raise

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        entry = self._entry
        if entry is None:
            return
        entry.lock.release()
        await self._pool._unreserve(self._key, entry)
        self._entry = None


class PerKeyLockPool(Generic[K]):
    def __init__(self) -> None:
        self._entries: dict[K, _LockEntry] = {}
        self._guard = asyncio.Lock()

    def acquire(self, key: K) -> _KeyedLockLease[K]:
        return _KeyedLockLease(self, key)

    async def _reserve(self, key: K) -> _LockEntry:
        async with self._guard:
            entry = self._entries.get(key)
            if entry is None:
                entry = _LockEntry(asyncio.Lock())
                self._entries[key] = entry
            entry.references += 1
            return entry

    async def _unreserve(self, key: K, entry: _LockEntry) -> None:
        async with self._guard:
            entry.references -= 1
            if entry.references == 0:
                del self._entries[key]


class SingleflightGroup(Generic[K, T]):
    def __init__(self) -> None:
        self._guard = asyncio.Lock()
        self._inflight: dict[K, asyncio.Future[T]] = {}

    async def do(
        self,
        key: K,
        factory: Callable[[], Awaitable[T]],
        *,
        on_leader: Callable[[], None] | None = None,
        on_follower: Callable[[], None] | None = None,
    ) -> T:
        async with self._guard:
            future = self._inflight.get(key)
            if future is None:
                future = asyncio.get_running_loop().create_future()
                self._inflight[key] = future
                leader = True
            else:
                leader = False

        if not leader:
            self._run_role_callback(on_follower)
            return await asyncio.shield(future)
        self._run_role_callback(on_leader)

        try:
            result = await factory()
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                future.cancel()
            else:
                future.set_exception(exc)
                future.exception()
            raise
        else:
            future.set_result(result)
            return result
        finally:
            await self._cleanup(key, future)

    @staticmethod
    def _run_role_callback(callback: Callable[[], None] | None) -> None:
        if callback is None:
            return
        try:
            callback()
        except Exception:
            return

    async def _cleanup(self, key: K, future: asyncio.Future[T]) -> None:
        async with self._guard:
            if self._inflight.get(key) is future:
                del self._inflight[key]
