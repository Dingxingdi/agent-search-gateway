import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from agent_search_gateway.config import ResolvedRatePolicy, ResolvedRateWindow


class RateLimitWaitTimeout(TimeoutError):
    def __init__(self, waited_seconds: float) -> None:
        super().__init__(waited_seconds)
        self.waited_seconds = waited_seconds


class RateGate:
    """Reserve permits against a provider's rolling limits on one event loop."""

    def __init__(
        self,
        policy: ResolvedRatePolicy,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._windows: list[tuple[ResolvedRateWindow, deque[float]]] = [
            (window, deque()) for window in policy.windows
        ]
        self._min_interval = policy.min_interval_seconds
        self._last_sent: float | None = None
        self._monotonic = monotonic
        self._sleep = sleep
        self._lock = asyncio.Lock()

    def next_available_in_seconds(self) -> float:
        """Estimate the current wait without reserving a permit or changing history."""
        return self._delay_at(self._monotonic())

    def _delay_at(self, now: float) -> float:
        delay = 0.0
        if self._last_sent is not None and self._min_interval is not None:
            delay = max(0.0, self._last_sent + self._min_interval - now)
        for window, sent in self._windows:
            if len(sent) == window.requests:
                delay = max(delay, sent[0] + window.period_seconds - now)
        return delay

    async def permit(
        self,
        *,
        timeout_seconds: float,
        before_reserve: Callable[[float], None] | None = None,
        budget: "SelectionBudget | None" = None,
    ) -> float:
        """Run synchronous dispatch preparation when eligible, then record the send time."""
        if not self._windows and self._min_interval is None:
            if before_reserve is not None:
                before_reserve(0.0)
            return 0.0
        started = self._monotonic()
        if budget is not None:
            timeout_seconds = min(timeout_seconds, budget.remaining_seconds)
        deadline = started + timeout_seconds
        has_slept = False
        while True:
            async with self._lock:
                now = self._monotonic()
                if has_slept and now > deadline:
                    raise RateLimitWaitTimeout(now - started)
                for window, sent in self._windows:
                    while sent and sent[0] + window.period_seconds <= now:
                        sent.popleft()
                delay = self._delay_at(now)
                if delay == 0:
                    waited = now - started
                    if before_reserve is not None:
                        before_reserve(waited)
                        now = self._monotonic()
                    for _, sent in self._windows:
                        sent.append(now)
                    self._last_sent = now
                    return waited
                if now >= deadline:
                    raise RateLimitWaitTimeout(now - started)
            has_slept = True
            wait_started = self._monotonic() if budget is not None else now
            try:
                await self._sleep(min(delay, deadline - now))
            finally:
                if budget is not None:
                    budget._charge_wait(self._monotonic() - wait_started)


def make_rate_gate(
    policy: ResolvedRatePolicy,
    *,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> RateGate | None:
    if not policy.windows and policy.min_interval_seconds is None:
        return None
    return RateGate(policy, monotonic=monotonic, sleep=sleep)


@dataclass(slots=True)
class SelectionBudget:
    remaining_seconds: float
    waited_seconds: float = 0.0

    @contextmanager
    def waiting(self, monotonic: Callable[[], float] = time.monotonic) -> Iterator[None]:
        """Measure one blocking interval in the coordinator, around its combined wait."""
        started = monotonic()
        deadline = started + self.remaining_seconds
        try:
            yield
        finally:
            finished = monotonic()
            self._charge_wait(finished - started)
        if finished > deadline:
            raise RateLimitWaitTimeout(self.waited_seconds)

    def _charge_wait(self, elapsed: float) -> None:
        self.waited_seconds += elapsed
        self.remaining_seconds = max(0.0, self.remaining_seconds - elapsed)


_BudgetBinding = tuple[asyncio.Task[object], RateGate, SelectionBudget]
_initial_budget: ContextVar[_BudgetBinding | None] = ContextVar("initial_rate_budget", default=None)


@contextmanager
def initial_rate_budget(
    gate: RateGate | None,
    budget: SelectionBudget | None = None,
    *,
    remaining_seconds: float | None = None,
    waited_seconds: float = 0.0,
) -> Iterator[None]:
    """Carry a fetch candidate's budget into its first matching HTTP permit."""
    owner = asyncio.current_task()
    binding = None
    if gate is not None and owner is not None:
        if budget is None:
            if remaining_seconds is None:
                raise TypeError("a selection budget or remaining_seconds is required")
            budget = SelectionBudget(remaining_seconds, waited_seconds)
        elif remaining_seconds is not None or waited_seconds:
            raise TypeError("pass either the selection budget or its initial durations")
        binding = (owner, gate, budget)
    token = _initial_budget.set(binding)
    try:
        yield
    finally:
        _initial_budget.reset(token)


def consume_initial_rate_budget(gate: RateGate) -> SelectionBudget | None:
    binding = _initial_budget.get()
    if binding is None:
        return None
    owner, bound_gate, budget = binding
    if owner is not asyncio.current_task() or bound_gate is not gate:
        return None
    _initial_budget.set(None)
    return budget
