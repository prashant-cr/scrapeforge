"""Per-domain concurrency limiting and polite delays.

Defaults are deliberately conservative: a handful of concurrent requests per
host, and a short randomized gap between them. Both are configurable, and both
apply per host rather than globally so that a scrape spanning many domains is
not throttled to the speed of one.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

__all__ = ["DomainRateLimiter"]


class DomainRateLimiter:
    """Bounds concurrency and spacing of requests, per host.

    Args:
        max_concurrency: Maximum simultaneous in-flight requests to one host.
        min_delay: Lower bound of the gap enforced between consecutive requests
            to the same host.
        max_delay: Upper bound of that gap. The actual delay is uniform in
            ``[min_delay, max_delay]`` so traffic is not perfectly periodic.
    """

    def __init__(
        self,
        *,
        max_concurrency: int = 4,
        min_delay: float = 0.5,
        max_delay: float = 1.5,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be >= 1")
        if min_delay < 0 or max_delay < min_delay:
            raise ValueError("require 0 <= min_delay <= max_delay")

        self._max_concurrency = max_concurrency
        self._min_delay = min_delay
        self._max_delay = max_delay

        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._next_allowed: dict[str, float] = {}
        self._host_locks: dict[str, asyncio.Lock] = {}
        self._registry_lock = asyncio.Lock()

    @staticmethod
    def _host(url: str) -> str:
        return (urlsplit(url).hostname or "").lower()

    async def _slots_for(self, host: str) -> tuple[asyncio.Semaphore, asyncio.Lock]:
        async with self._registry_lock:
            semaphore = self._semaphores.setdefault(host, asyncio.Semaphore(self._max_concurrency))
            lock = self._host_locks.setdefault(host, asyncio.Lock())
            return semaphore, lock

    async def _wait_turn(self, host: str, lock: asyncio.Lock) -> None:
        """Sleep until this host's next slot, then reserve the one after it."""
        if self._max_delay <= 0:
            return
        async with lock:
            now = time.monotonic()
            earliest = self._next_allowed.get(host, 0.0)
            if earliest > now:
                await asyncio.sleep(earliest - now)
            delay = random.uniform(self._min_delay, self._max_delay)
            self._next_allowed[host] = max(time.monotonic(), earliest) + delay

    @asynccontextmanager
    async def slot(self, url: str) -> AsyncIterator[None]:
        """Acquire a request slot for ``url``'s host.

        Blocks until both the concurrency limit and the politeness delay allow
        the request to proceed.
        """
        host = self._host(url)
        semaphore, lock = await self._slots_for(host)
        async with semaphore:
            await self._wait_turn(host, lock)
            yield
