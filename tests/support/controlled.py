"""Event-driven concurrency helpers for tests."""

import asyncio
from typing import TypeVar

T = TypeVar("T")


class ControlledProvider:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.active = 0
        self.max_active = 0
        self.calls = 0

    async def run(self, result: T) -> T:
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.entered.set()
        try:
            await self.release.wait()
            return result
        finally:
            self.active -= 1


class ManualClock:
    """An event-loop clock advanced by the test, independently of its sleepers."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleepers: dict[asyncio.Future[None], float] = {}

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        alarm: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.sleepers[alarm] = self.now + seconds
        try:
            await alarm
        finally:
            del self.sleepers[alarm]

    async def advance_to(self, now: float, *, wake_early: bool = False) -> None:
        assert now >= self.now
        self.now = now
        for alarm, deadline in self.sleepers.items():
            if (wake_early or deadline <= now) and not alarm.done():
                alarm.set_result(None)
        await asyncio.sleep(0)
