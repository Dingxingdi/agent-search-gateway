"""Select and reserve members of a logical provider's credential pool."""

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence, Set
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Generic, Literal, TypeVar

from .concurrency import CapacityGate, CapacityLease, join_cleanup
from .config import CredentialStrategy
from .errors import ErrorCode, ExecutionFailure
from .observability import CredentialIdentity, credential_context, log_event, seconds_to_ms
from .rate_limit import RateGate, RateLimitWaitTimeout, SelectionBudget, initial_rate_budget

A = TypeVar("A")
T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class CredentialMember(Generic[A]):
    adapter: A = field(repr=False)
    capacity: CapacityGate | None = None
    rate: RateGate | None = None


@dataclass(slots=True, repr=False)
class CredentialReservation(Generic[A]):
    index: int
    member: CredentialMember[A]
    _pool: "CredentialPool[A]"
    _lease: CapacityLease | None = None
    _state: Literal["reserved", "active", "closed"] = field(default="reserved", init=False)

    _cleanup: asyncio.Task[None] | None = field(default=None, init=False)

    @asynccontextmanager
    async def consume(self) -> AsyncIterator[CredentialMember[A]]:
        """Transfer the reservation to one operation until it leaves this context."""
        if self._state != "reserved":
            raise RuntimeError("credential reservation is no longer available")
        self._state = "active"
        try:
            yield self.member
        finally:
            self._state = "closed"
            await self.release()

    async def release(self) -> None:
        if self._state == "active":
            return
        self._state = "closed"
        if self._lease is None:
            return
        if self._cleanup is None:
            self._cleanup = asyncio.create_task(self._lease.release())
        # Even repeated cancellation must leave the slot free before it propagates.
        cancelled = None
        while not self._cleanup.done():
            try:
                await asyncio.shield(self._cleanup)
            except asyncio.CancelledError as exc:
                cancelled = exc
        self._cleanup.result()
        if cancelled is not None:
            raise cancelled


class CredentialPool(Generic[A]):
    def __init__(
        self,
        namespace: str,
        name: str,
        members: Sequence[CredentialMember[A]],
        *,
        strategy: CredentialStrategy = "round_robin",
        request_timeout_seconds: float = 30.0,
        error_code: ErrorCode = ErrorCode.ALL_PROVIDERS_FAILED,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.namespace = namespace
        self.name = name
        self._members = tuple(members)
        self._strategy = strategy
        self._request_timeout_seconds = request_timeout_seconds
        self._error_code = error_code
        self._monotonic = monotonic
        self._sleep = sleep
        self._cursor = 0
        self._selection_lock = asyncio.Lock()

    def _identity(self, index: int) -> CredentialIdentity:
        return CredentialIdentity(self.namespace, self.name, index)

    def _log(self, event: str, index: int | None = None, **fields: object) -> None:
        with credential_context(self._identity(index) if index is not None else None):
            log_event(
                logging.getLogger(__name__),
                logging.DEBUG,
                event,
                namespace=self.namespace,
                provider=self.name,
                exc_info=False,
                **fields,
            )

    async def try_reserve(self, attempted: Set[int]) -> CredentialReservation[A] | None:
        async with self._selection_lock:
            size = len(self._members)
            for offset in range(size):
                index = (self._cursor + offset) % size
                if index in attempted:
                    continue
                member = self._members[index]
                if member.rate is not None and member.rate.next_available_in_seconds() > 0:
                    continue
                lease = None
                if member.capacity is not None:
                    lease = await member.capacity.try_lease()
                    if lease is None:
                        continue
                if self._strategy == "round_robin":
                    self._cursor = (index + 1) % size
                return CredentialReservation(index, member, self, lease)
            return None

    async def wait_available(
        self,
        attempted: Set[int],
        budget: SelectionBudget,
        *,
        on_rate_wait: Callable[[float], None] | None = None,
    ) -> None:
        """Observe an eligible member without claiming its capacity or rate permit."""
        while True:
            full: list[CapacityGate] = []
            earliest_rate = None
            for index, member in enumerate(self._members):
                if index in attempted:
                    continue
                if member.rate is not None:
                    now = self._monotonic()
                    delay = member.rate.next_available_in_seconds()
                    if delay > 0:
                        self._log(
                            "credential_waiting",
                            index,
                            reason="rate",
                            estimated_wait_ms=seconds_to_ms(delay),
                        )
                        ready_at = now + delay
                        if earliest_rate is None or ready_at < earliest_rate:
                            earliest_rate = ready_at
                        continue
                if member.capacity is None or member.capacity.in_use < member.capacity.limit:
                    return
                self._log("credential_waiting", index, reason="capacity")
                full.append(member.capacity)

            if not full and earliest_rate is None:
                raise ExecutionFailure(self._error_code, "No unattempted credential")
            if budget.remaining_seconds <= 0:
                break
            deadline = self._monotonic() + budget.remaining_seconds
            if earliest_rate is not None:
                deadline = min(deadline, earliest_rate)
                if on_rate_wait is not None:
                    on_rate_wait(max(0, earliest_rate - self._monotonic()))
            waiters: list[asyncio.Task[None]] = []
            try:
                for gate in full:
                    waiters.append(asyncio.create_task(gate.wait_until_available()))
                waiters.append(asyncio.create_task(self._wake_at(deadline)))
                with budget.waiting(self._monotonic):
                    done, _ = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
                for waiter in done:
                    waiter.result()
            except RateLimitWaitTimeout:
                break
            finally:
                for waiter in waiters:
                    if not waiter.done():
                        waiter.cancel()
                await join_cleanup(asyncio.create_task(self._join_waiters(waiters)))

        raise ExecutionFailure(
            self._error_code,
            "Provider availability wait timed out",
            reason="rate_limit_wait_timeout",
        )

    @staticmethod
    async def _join_waiters(waiters: Sequence[asyncio.Task[None]]) -> None:
        await asyncio.gather(*waiters, return_exceptions=True)

    async def _wake_at(self, deadline: float) -> None:
        delay = deadline - self._monotonic()
        if delay > 0:
            await self._sleep(delay)

    async def invoke(
        self,
        operation: Callable[[A], Awaitable[T]],
        *,
        first: CredentialReservation[A] | None = None,
        budget: SelectionBudget | None = None,
    ) -> T:
        if first is not None and first._pool is not self:
            raise ValueError("reservation belongs to another pool")
        if budget is None:
            budget = SelectionBudget(self._request_timeout_seconds)
        attempted: set[int] = set()
        last_failure = None
        reservation = first
        try:
            while True:
                # Uncontended gates and adapter failures can finish without yielding.
                # Deliver pending cancellation before selecting or dispatching another member.
                await asyncio.sleep(0)
                if len(attempted) == len(self._members):
                    if last_failure is not None:
                        raise last_failure
                    self._log("credential_exhausted", reason="no_available_credential")
                    raise ExecutionFailure(self._error_code, "No available credential")
                if reservation is None:
                    reservation = await self.try_reserve(attempted)
                if reservation is None:
                    try:
                        await self.wait_available(attempted, budget)
                    except ExecutionFailure:
                        self._log("credential_exhausted", reason="availability_timeout")
                        if last_failure is not None:
                            raise last_failure from None
                        raise
                    continue
                index = reservation.index
                attempted.add(index)
                self._log("credential_selected", index)
                async with reservation.consume() as member:
                    try:
                        with (
                            credential_context(self._identity(index)),
                            initial_rate_budget(member.rate, budget),
                        ):
                            result = await operation(member.adapter)
                    except ExecutionFailure as exc:
                        last_failure = exc
                        self._log(
                            "credential_exhausted"
                            if len(attempted) == len(self._members)
                            else "credential_fallback",
                            index,
                            reason="execution_failure",
                        )
                    else:
                        # An ungated adapter may request cancellation and return synchronously.
                        await asyncio.sleep(0)
                        return result
                reservation = None
        except asyncio.CancelledError:
            self._log(
                "credential_cancelled", reservation.index if reservation is not None else None
            )
            raise
        finally:
            if reservation is not None:
                await reservation.release()
