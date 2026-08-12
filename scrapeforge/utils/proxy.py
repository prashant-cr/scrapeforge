"""Proxy URL helpers.

Proxy credentials are never logged. :func:`redact_proxy` produces the only form
safe to put in a log line or an exception message.
"""

from __future__ import annotations

from typing import NamedTuple
from urllib.parse import urlsplit, urlunsplit

__all__ = ["ProxyParts", "parse_proxy", "redact_proxy"]


class ProxyParts(NamedTuple):
    """A proxy URL split into a credential-free server plus separate auth."""

    server: str
    username: str | None
    password: str | None


def parse_proxy(proxy: str | None) -> ProxyParts | None:
    """Split ``scheme://user:pass@host:port`` into server and credentials.

    Playwright (and some other clients) require the credentials to be passed
    separately from the server URL.

    Args:
        proxy: Proxy URL, or ``None``.

    Returns:
        A :class:`ProxyParts`, or ``None`` when ``proxy`` is falsy.
    """
    if not proxy:
        return None

    parts = urlsplit(proxy if "://" in proxy else f"http://{proxy}")
    netloc = parts.hostname or ""
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    server = urlunsplit((parts.scheme or "http", netloc, "", "", ""))
    return ProxyParts(server=server, username=parts.username, password=parts.password)


def redact_proxy(proxy: str | None) -> str | None:
    """Return the proxy URL with any credentials replaced by ``***``."""
    parts = parse_proxy(proxy)
    if parts is None:
        return None
    if parts.username or parts.password:
        scheme, _, rest = parts.server.partition("://")
        return f"{scheme}://***@{rest}"
    return parts.server
