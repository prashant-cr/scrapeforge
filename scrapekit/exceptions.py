"""Exception hierarchy for scrapekit.

All errors raised by the public API derive from :class:`ScrapekitError`, so callers
never have to catch library-internal exceptions (``httpx.ConnectError``,
``playwright.Error``, provider SDK errors, ...).

::

    ScrapekitError
    ├── FetchError
    │   ├── AllStrategiesFailed
    │   ├── ChallengeError
    │   └── ProxyError
    ├── ParseError
    └── ConfigError
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "AllStrategiesFailed",
    "ChallengeError",
    "ConfigError",
    "FetchError",
    "ParseError",
    "ProxyError",
    "ScrapekitError",
]

_SNIPPET_LIMIT = 500


def _snippet(body: str | bytes | None) -> str | None:
    """Truncate a response body for safe inclusion in an error message."""
    if body is None:
        return None
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")
    body = " ".join(body.split())
    if len(body) > _SNIPPET_LIMIT:
        return body[:_SNIPPET_LIMIT] + "..."
    return body


class ScrapekitError(Exception):
    """Base class for every error raised by scrapekit."""


class FetchError(ScrapekitError):
    """A fetch attempt failed.

    Attributes:
        url: The URL that was being fetched.
        status_code: Last HTTP status observed, if any.
        strategy_used: Name of the fetcher that produced the failure, if any.
        body_snippet: Truncated response body, useful for debugging blocks.
    """

    def __init__(
        self,
        message: str,
        *,
        url: str | None = None,
        status_code: int | None = None,
        strategy_used: str | None = None,
        body: str | bytes | None = None,
    ) -> None:
        self.url = url
        self.status_code = status_code
        self.strategy_used = strategy_used
        self.body_snippet = _snippet(body)

        details = []
        if url:
            details.append(f"url={url}")
        if status_code is not None:
            details.append(f"status={status_code}")
        if strategy_used:
            details.append(f"strategy={strategy_used}")
        if details:
            message = f"{message} ({', '.join(details)})"
        if self.body_snippet:
            message = f"{message}\nbody: {self.body_snippet}"
        super().__init__(message)


class AllStrategiesFailed(FetchError):
    """Every enabled strategy was tried and none produced a usable response.

    Attributes:
        attempts: Mapping of strategy name to the error it raised (or a short
            reason string when the strategy was skipped).
    """

    def __init__(
        self, message: str, *, url: str | None = None, attempts: dict[str, Any] | None = None
    ) -> None:
        self.attempts = attempts or {}
        if self.attempts:
            rendered = "; ".join(f"{name}: {err}" for name, err in self.attempts.items())
            message = f"{message} [{rendered}]"
        super().__init__(message, url=url)


class ChallengeError(FetchError):
    """The response looks like a bot-management interstitial or block page.

    scrapekit deliberately does not attempt to solve challenges. Catch this and
    plug in your own solver, a residential proxy, or back off.

    Attributes:
        signature: The heuristic that matched, for debugging.
    """

    def __init__(self, message: str, *, signature: str | None = None, **kwargs: Any) -> None:
        self.signature = signature
        if signature:
            message = f"{message} [matched: {signature}]"
        super().__init__(message, **kwargs)


class ProxyError(FetchError):
    """The configured proxy could not be used (unreachable, refused, bad auth)."""


class ParseError(ScrapekitError):
    """Extraction failed or the LLM output did not validate against the schema.

    Attributes:
        schema: Name of the target schema.
        validation_error: The underlying validation error, if any.
    """

    def __init__(
        self,
        message: str,
        *,
        schema: str | None = None,
        validation_error: Exception | None = None,
    ) -> None:
        self.schema = schema
        self.validation_error = validation_error
        if schema:
            message = f"{message} (schema={schema})"
        super().__init__(message)


class ConfigError(ScrapekitError):
    """Invalid configuration: unknown strategy, missing API key, missing extra."""
