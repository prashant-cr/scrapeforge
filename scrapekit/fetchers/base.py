"""The fetcher interface every strategy implements."""

from __future__ import annotations

import importlib.util
import random
import time
from abc import ABC, abstractmethod

from ..config import FetchOptions, ScraperConfig
from ..fingerprint import UserAgentProfile, profile_for_user_agent, random_profile
from ..models import ContentType, FetchResponse
from ..utils.detect import detect_content_type

__all__ = ["BaseFetcher", "module_available"]

#: Fixed seed so ``rotate_user_agent=False`` yields the same profile every run.
_STABLE_RNG = random.Random(0)


def module_available(name: str) -> bool:
    """Return whether an optional dependency can be imported."""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):  # pragma: no cover - malformed install
        return False


class BaseFetcher(ABC):
    """Common interface for every fetch strategy.

    Subclasses implement :meth:`fetch` and set :attr:`name`. The chain uses
    :meth:`is_available` to skip strategies whose optional dependency is not
    installed, rather than failing at import time.

    Args:
        config: Client-wide configuration.
    """

    #: Registry key and value reported as ``FetchResponse.strategy_used``.
    name: str = "base"

    #: Optional dependency required by this fetcher (``None`` = always available).
    requires: str | None = None

    #: Human-readable install hint used in the error when the extra is missing.
    extra_name: str | None = None

    def __init__(self, config: ScraperConfig) -> None:
        self.config = config

    @abstractmethod
    async def fetch(self, url: str, options: FetchOptions) -> FetchResponse:
        """Perform the request and return a normalized response.

        Args:
            url: Absolute URL to fetch.
            options: Fully-resolved per-request options (see
                :meth:`~scrapekit.config.FetchOptions.resolve`).

        Returns:
            A :class:`~scrapekit.models.FetchResponse`.

        Raises:
            FetchError: On any transport-level failure. Subclasses translate
                library-specific exceptions into the scrapekit hierarchy.
        """

    def is_available(self) -> bool:
        """Return whether this fetcher's dependencies are installed."""
        return self.requires is None or module_available(self.requires)

    async def aclose(self) -> None:  # noqa: B027 - optional hook, not abstract
        """Release any long-lived resources. Safe to call more than once."""

    # -- helpers for subclasses -------------------------------------------

    def select_profile(self, options: FetchOptions) -> UserAgentProfile:
        """Choose the user-agent profile for this request.

        A caller-pinned ``User-Agent`` header always wins; otherwise a profile is
        drawn from the bundled pool when rotation is enabled, or the first pool
        entry is used for a stable fingerprint when it is not.
        """
        pinned = None
        for key, value in (options.headers or {}).items():
            if key.lower() == "user-agent":
                pinned = value
                break
        if pinned:
            return profile_for_user_agent(pinned)
        if self.config.rotate_user_agent:
            return random_profile()
        return random_profile(rng=_STABLE_RNG)

    def build_response(
        self,
        *,
        url: str,
        request_url: str,
        status_code: int,
        headers: dict[str, str],
        content: bytes,
        started: float,
        encoding: str = "utf-8",
    ) -> FetchResponse:
        """Assemble a :class:`~scrapekit.models.FetchResponse` from raw parts."""
        normalized = {str(k).lower(): str(v) for k, v in headers.items()}
        content_type = detect_content_type(normalized.get("content-type"), content)
        return FetchResponse(
            url=url,
            request_url=request_url,
            status_code=status_code,
            headers=normalized,
            content=content,
            content_type=content_type,
            strategy_used=self.name,
            encoding=encoding or "utf-8",
            elapsed=time.monotonic() - started,
        )

    @staticmethod
    def expected_type(options: FetchOptions) -> ContentType | None:
        return options.expected_content_type

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} name={self.name!r}>"
