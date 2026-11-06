from __future__ import annotations

import asyncio

import pytest

from biblio_search.ratelimit import RateLimiter


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_spaces_consecutive_requests() -> None:
    clock = FakeClock()
    limiter = RateLimiter(4, clock=clock, sleep=clock.sleep)

    async def three() -> None:
        for _ in range(3):
            await limiter.acquire()

    asyncio.run(three())
    assert clock.sleeps == pytest.approx([0.25, 0.25])


def test_does_not_wait_after_idle_time() -> None:
    clock = FakeClock()
    limiter = RateLimiter(2, clock=clock, sleep=clock.sleep)

    async def scenario() -> None:
        await limiter.acquire()
        clock.now += 5
        await limiter.acquire()

    asyncio.run(scenario())
    assert clock.sleeps == []


def test_concurrent_callers_are_serialised() -> None:
    clock = FakeClock()
    limiter = RateLimiter(10, clock=clock, sleep=clock.sleep)

    async def scenario() -> None:
        await asyncio.gather(*(limiter.acquire() for _ in range(5)))

    asyncio.run(scenario())
    assert sum(clock.sleeps) == pytest.approx(0.4)


def test_rejects_non_positive_rate() -> None:
    with pytest.raises(ValueError, match="positive"):
        RateLimiter(0)
