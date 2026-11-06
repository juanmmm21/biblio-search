"""Async rate limiter for APIs with a requests-per-second quota."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable


class RateLimiter:
    """Spaces request starts so that at most ``rate_per_second`` begin in any second.

    Requests are serialised: NCBI counts concurrent requests against the same quota, so
    letting them overlap would only trade throughput for 429 responses.
    """

    def __init__(
        self,
        rate_per_second: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if rate_per_second <= 0:
            raise ValueError("rate_per_second must be positive")
        self._interval = 1.0 / rate_per_second
        self._clock = clock
        self._sleep = sleep
        self._next_start = float("-inf")
        self._lock = asyncio.Lock()

    @property
    def interval(self) -> float:
        return self._interval

    async def acquire(self) -> None:
        """Wait until the next request is allowed to start."""
        async with self._lock:
            now = self._clock()
            wait = self._next_start - now
            if wait > 0:
                await self._sleep(wait)
                now = self._next_start
            self._next_start = now + self._interval
