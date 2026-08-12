"""robots.txt awareness.

On by default (``ScraperConfig.respect_robots``). Disabling it is the caller's
explicit, documented choice — see the responsible-use section of the README.

A missing or unreachable ``robots.txt`` is treated as *allowed*, matching the
behaviour of every mainstream crawler: absence of a policy is not a prohibition.
"""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx

__all__ = ["RobotsCache"]

logger = logging.getLogger(__name__)


class RobotsCache:
    """Fetches and caches ``robots.txt`` per origin.

    One parser is kept per ``scheme://host:port``. Concurrent callers for the
    same origin share a single fetch.

    Args:
        timeout: Seconds to wait for the ``robots.txt`` request.
        user_agent: Product token matched against ``User-agent:`` groups.
    """

    def __init__(self, *, timeout: float = 10.0, user_agent: str = "*") -> None:
        self._timeout = timeout
        self._user_agent = user_agent
        self._parsers: dict[str, RobotFileParser | None] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()

    @staticmethod
    def _origin(url: str) -> str:
        parts = urlsplit(url)
        return urlunsplit((parts.scheme, parts.netloc, "", "", ""))

    async def _lock_for(self, origin: str) -> asyncio.Lock:
        async with self._global_lock:
            return self._locks.setdefault(origin, asyncio.Lock())

    async def _load(
        self, origin: str, *, proxy: str | None, verify_ssl: bool
    ) -> RobotFileParser | None:
        robots_url = f"{origin}/robots.txt"
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                follow_redirects=True,
                proxy=proxy,
                verify=verify_ssl,
            ) as client:
                response = await client.get(robots_url)
        except httpx.HTTPError as exc:
            logger.debug(
                "robots.txt unreachable for %s (%s); treating as allowed",
                origin,
                type(exc).__name__,
            )
            return None

        if response.status_code >= 400:
            logger.debug(
                "robots.txt returned %s for %s; treating as allowed", response.status_code, origin
            )
            return None

        parser = RobotFileParser()
        parser.parse(response.text.splitlines())
        return parser

    async def allowed(
        self,
        url: str,
        *,
        user_agent: str | None = None,
        proxy: str | None = None,
        verify_ssl: bool = True,
    ) -> bool:
        """Return whether ``url`` may be fetched according to its ``robots.txt``.

        Args:
            url: The URL about to be requested.
            user_agent: Override the product token for this check.
            proxy: Proxy to use when fetching ``robots.txt``.
            verify_ssl: TLS verification for the ``robots.txt`` request.

        Returns:
            ``True`` if allowed or if no policy could be retrieved.
        """
        origin = self._origin(url)

        if origin not in self._parsers:
            lock = await self._lock_for(origin)
            async with lock:
                if origin not in self._parsers:
                    self._parsers[origin] = await self._load(
                        origin, proxy=proxy, verify_ssl=verify_ssl
                    )

        parser = self._parsers[origin]
        if parser is None:
            return True
        return parser.can_fetch(user_agent or self._user_agent, url)

    def clear(self) -> None:
        """Drop all cached policies."""
        self._parsers.clear()
