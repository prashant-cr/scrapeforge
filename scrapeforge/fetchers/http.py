"""Plain async HTTP via httpx — the cheapest rung of the fallback chain."""

from __future__ import annotations

import time

import httpx

from ..config import FetchOptions
from ..exceptions import FetchError, ProxyError
from ..fingerprint import build_headers
from ..models import FetchResponse
from .base import BaseFetcher

__all__ = ["HttpxFetcher"]


class HttpxFetcher(BaseFetcher):
    """Fetch over plain HTTP/2 with a rotated UA and a coherent header set.

    Fast and cheap, and sufficient for open APIs and pages without a bot wall.
    It does not impersonate a browser's TLS fingerprint — that is the next rung
    (:class:`~scrapeforge.fetchers.impersonate.CurlCffiFetcher`).
    """

    name = "http"

    async def fetch(self, url: str, options: FetchOptions) -> FetchResponse:
        profile = self.select_profile(options)
        headers = build_headers(
            profile,
            expected_content_type=self.expected_type(options),
            is_navigation=not options.has_body(),
            extra=options.headers,
        )
        started = time.monotonic()

        try:
            async with httpx.AsyncClient(
                http2=True,
                timeout=options.timeout,
                follow_redirects=bool(options.follow_redirects),
                proxy=options.proxy,
                verify=bool(options.verify_ssl),
                cookies=options.cookies or None,
            ) as client:
                response = await client.request(
                    options.method,
                    url,
                    params=options.params,
                    data=options.data if not isinstance(options.data, (str, bytes)) else None,
                    content=options.data if isinstance(options.data, (str, bytes)) else None,
                    json=options.json_body,
                    headers=headers,
                )
        except httpx.ProxyError as exc:
            raise ProxyError(
                f"Proxy connection failed: {type(exc).__name__}", url=url, strategy_used=self.name
            ) from exc
        except httpx.HTTPError as exc:
            raise FetchError(
                f"httpx request failed: {type(exc).__name__}: {exc}",
                url=url,
                strategy_used=self.name,
            ) from exc

        return self.build_response(
            url=str(response.url),
            request_url=url,
            status_code=response.status_code,
            headers=dict(response.headers),
            content=response.content,
            started=started,
            encoding=response.encoding or "utf-8",
        )
