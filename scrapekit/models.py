"""Normalized response objects shared by every fetcher."""

from __future__ import annotations

import json as _json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

__all__ = ["ContentType", "FetchResponse"]


class ContentType(str, Enum):
    """Resolved body type of a response."""

    HTML = "html"
    JSON = "json"
    XML = "xml"
    TEXT = "text"
    BINARY = "binary"
    UNKNOWN = "unknown"


@dataclass
class FetchResponse:
    """A normalized HTTP response, independent of which fetcher produced it.

    Attributes:
        url: Final URL after redirects.
        status_code: HTTP status code.
        headers: Response headers with lower-cased keys.
        content: Raw response body.
        content_type: Resolved :class:`ContentType` (header first, then sniffing).
        strategy_used: Name of the fetcher that produced this response.
        encoding: Character encoding used to decode :attr:`text`.
        elapsed: Wall-clock seconds spent on the request.
        request_url: The URL originally requested.
    """

    url: str
    status_code: int
    headers: dict[str, str] = field(default_factory=dict)
    content: bytes = b""
    content_type: ContentType = ContentType.UNKNOWN
    strategy_used: str = ""
    encoding: str = "utf-8"
    elapsed: float = 0.0
    request_url: str = ""

    def __post_init__(self) -> None:
        self.headers = {str(k).lower(): str(v) for k, v in self.headers.items()}
        if not self.request_url:
            self.request_url = self.url

    @property
    def ok(self) -> bool:
        """True when the status code is in the 2xx range."""
        return 200 <= self.status_code < 300

    @property
    def text(self) -> str:
        """Body decoded to ``str``, replacing undecodable bytes."""
        return self.content.decode(self.encoding, errors="replace")

    def json(self) -> Any:
        """Parse the body as JSON.

        Raises:
            ValueError: If the body is not valid JSON.
        """
        return _json.loads(self.text)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<FetchResponse {self.status_code} {self.url!r} "
            f"type={self.content_type.value} via={self.strategy_used!r} "
            f"bytes={len(self.content)}>"
        )
