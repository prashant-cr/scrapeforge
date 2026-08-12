"""The fallback chain: try strategies in order, escalating on blocks.

Two failure classes are handled differently, and the distinction is the point of
this module:

* **Transient** (timeout, connection reset, DNS blip) — retried on the *same*
  strategy with exponential backoff and jitter.
* **Blocked** (challenge page, WAF status, wrong content type) — escalated to the
  *next* strategy immediately. Burning retries against a bot wall only wastes
  time and makes the traffic pattern worse.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from tenacity import AsyncRetrying, RetryError, stop_after_attempt, wait_exponential_jitter

from ..config import FetchOptions, ScraperConfig
from ..exceptions import AllStrategiesFailed, ChallengeError, ConfigError, FetchError, ProxyError
from ..models import FetchResponse
from ..utils.detect import classify_response
from ..utils.proxy import redact_proxy
from .base import BaseFetcher
from .browser import PlaywrightFetcher
from .http import HttpxFetcher
from .impersonate import CurlCffiFetcher
from .tls import TlsClientFetcher

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Mapping

__all__ = ["FETCHER_REGISTRY", "FallbackChain", "register_fetcher"]

logger = logging.getLogger(__name__)

#: Name -> fetcher class. Every fetcher registers here and is addressed by name
#: in ``ScraperConfig.strategies``.
FETCHER_REGISTRY: dict[str, type[BaseFetcher]] = {
    HttpxFetcher.name: HttpxFetcher,
    CurlCffiFetcher.name: CurlCffiFetcher,
    TlsClientFetcher.name: TlsClientFetcher,
    PlaywrightFetcher.name: PlaywrightFetcher,
}


def register_fetcher(fetcher_cls: type[BaseFetcher]) -> type[BaseFetcher]:
    """Register a custom fetcher so it can be named in ``strategies``.

    Usable as a decorator::

        @register_fetcher
        class MyFetcher(BaseFetcher):
            name = "mine"
            async def fetch(self, url, options): ...

    Args:
        fetcher_cls: A :class:`~scrapesmith.fetchers.base.BaseFetcher` subclass
            with a unique ``name``.

    Returns:
        The class, unchanged.

    Raises:
        ConfigError: If ``name`` is missing or already taken by a different class.
    """
    name = getattr(fetcher_cls, "name", None)
    if not name or name == "base":
        raise ConfigError(f"{fetcher_cls.__name__} must define a unique 'name' class attribute")
    existing = FETCHER_REGISTRY.get(name)
    if existing is not None and existing is not fetcher_cls:
        raise ConfigError(f"Strategy name {name!r} is already registered to {existing.__name__}")
    FETCHER_REGISTRY[name] = fetcher_cls
    return fetcher_cls


def _is_retryable(exc: BaseException) -> bool:
    """Transient transport failures are retryable; blocks and bad config are not."""
    if isinstance(exc, (ChallengeError, ProxyError, ConfigError)):
        return False
    return isinstance(exc, FetchError)


class FallbackChain:
    """Runs enabled fetchers in order until one returns a usable response.

    Args:
        config: Client-wide configuration. ``config.strategies`` sets the default
            escalation order; a per-request override is honoured.

    Raises:
        ConfigError: If ``config.strategies`` names an unregistered strategy.
    """

    def __init__(self, config: ScraperConfig) -> None:
        self.config = config
        self._instances: dict[str, BaseFetcher] = {}
        self._validate(config.strategies)

    def _validate(self, strategies: list[str]) -> None:
        """Reject strategy names that are neither registered nor already built."""
        unknown = [s for s in strategies if s not in FETCHER_REGISTRY and s not in self._instances]
        if unknown:
            raise ConfigError(
                f"Unknown strategy/strategies: {', '.join(unknown)}. "
                f"Registered: {', '.join(sorted(FETCHER_REGISTRY))}"
            )

    def get_fetcher(self, name: str) -> BaseFetcher:
        """Return the (lazily constructed, cached) fetcher for ``name``."""
        if name not in self._instances:
            self._validate([name])
            self._instances[name] = FETCHER_REGISTRY[name](self.config)
        return self._instances[name]

    async def aclose(self) -> None:
        """Close every instantiated fetcher."""
        for fetcher in self._instances.values():
            await fetcher.aclose()
        self._instances.clear()

    async def _attempt(
        self, fetcher: BaseFetcher, url: str, options: FetchOptions
    ) -> FetchResponse:
        """Run one strategy with retries, then validate the response.

        Raises:
            ChallengeError: If the response looks like a block or challenge.
            FetchError: On transport failure after retries are exhausted.
        """
        attempts = max(1, self.config.max_retries + 1)
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(attempts),
                wait=wait_exponential_jitter(initial=0.5, max=8.0, jitter=0.5),
                retry=lambda state: (
                    _is_retryable(state.outcome.exception())
                    if state.outcome and state.outcome.failed
                    else False
                ),
                reraise=True,
            ):
                with attempt:
                    response = await fetcher.fetch(url, options)
        except RetryError as exc:  # pragma: no cover - reraise=True makes this rare
            raise FetchError(
                f"{fetcher.name} exhausted retries", url=url, strategy_used=fetcher.name
            ) from exc

        usable, reason = classify_response(
            status_code=response.status_code,
            body=response.content,
            content_type=response.content_type,
            expected_content_type=options.expected_content_type,
        )
        if not usable:
            raise ChallengeError(
                f"{fetcher.name} response looks blocked or challenged",
                signature=reason,
                url=response.url,
                status_code=response.status_code,
                strategy_used=fetcher.name,
                body=response.content,
            )
        return response

    async def fetch(self, url: str, options: FetchOptions) -> FetchResponse:
        """Fetch ``url``, escalating through strategies until one succeeds.

        Args:
            url: Absolute URL.
            options: Fully-resolved options (see
                :meth:`~scrapesmith.config.FetchOptions.resolve`).

        Returns:
            The first usable :class:`~scrapesmith.models.FetchResponse`.

        Raises:
            AllStrategiesFailed: When every enabled strategy failed or was
                skipped. ``.attempts`` records the reason for each.
            ConfigError: If a named strategy is not registered.
        """
        strategies = list(options.strategies or self.config.strategies)
        self._validate(strategies)

        attempts: dict[str, str] = {}
        last_challenge: ChallengeError | None = None

        for name in strategies:
            fetcher = self.get_fetcher(name)

            if not fetcher.is_available():
                hint = fetcher.extra_name or name
                attempts[name] = f"skipped (dependency missing; install {hint})"
                logger.debug("Skipping strategy %r: dependency not installed", name)
                continue

            logger.debug(
                "Trying strategy %r for %s (proxy=%s)", name, url, redact_proxy(options.proxy)
            )
            try:
                response = await self._attempt(fetcher, url, options)
            except ChallengeError as exc:
                last_challenge = exc
                attempts[name] = f"blocked ({exc.signature or 'challenge'})"
                logger.debug("Strategy %r blocked (%s); escalating", name, exc.signature)
                continue
            except ConfigError as exc:
                attempts[name] = f"unavailable ({exc})"
                logger.debug("Strategy %r unavailable: %s", name, exc)
                continue
            except FetchError as exc:
                attempts[name] = f"{type(exc).__name__}: {exc}"
                logger.debug("Strategy %r failed: %s", name, exc)
                continue

            logger.debug("Strategy %r succeeded with status %s", name, response.status_code)
            return response

        # Every strategy was blocked and none errored for another reason: the
        # honest answer is "you are being challenged", not "the network failed".
        if last_challenge is not None and all("blocked" in reason for reason in attempts.values()):
            raise ChallengeError(
                "All strategies were blocked by bot management",
                signature=last_challenge.signature,
                url=url,
                status_code=last_challenge.status_code,
                strategy_used=last_challenge.strategy_used,
            )

        raise AllStrategiesFailed("All fetch strategies failed", url=url, attempts=attempts)

    def describe(self) -> Mapping[str, bool]:
        """Return ``{strategy: available}`` for the configured strategies."""
        return {name: self.get_fetcher(name).is_available() for name in self.config.strategies}
