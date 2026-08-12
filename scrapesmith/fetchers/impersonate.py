"""TLS/JA3 impersonation via curl_cffi.

This is the highest-value rung of the chain relative to its cost. curl_cffi
replays a real browser's TLS ClientHello — cipher and extension ordering, ALPN,
supported groups — plus its HTTP/2 SETTINGS and header frame ordering. A large
share of fingerprint-based blocking clears here without paying for a browser.
"""

from __future__ import annotations

import time

from ..config import FetchOptions
from ..exceptions import FetchError, ProxyError
from ..fingerprint import build_headers
from ..models import FetchResponse
from .base import BaseFetcher

__all__ = ["CurlCffiFetcher"]


class CurlCffiFetcher(BaseFetcher):
    """Fetch with a browser-matched TLS/HTTP2 fingerprint.

    The impersonation target comes from ``ScraperConfig.impersonate_target``.
    When user-agent rotation is on, the target is instead taken from the chosen
    profile so the TLS fingerprint and the ``User-Agent`` agree — claiming to be
    Chrome while handshaking like curl is worse than not impersonating at all.
    """

    name = "impersonate"
    requires = "curl_cffi"
    extra_name = "scrapesmith (core dependency)"

    def _impersonate_target(self, options: FetchOptions) -> str:
        if self.config.rotate_user_agent:
            return self.select_profile(options).impersonate
        return self.config.impersonate_target

    async def fetch(self, url: str, options: FetchOptions) -> FetchResponse:
        try:
            from curl_cffi import requests as curl_requests
        except ImportError as exc:  # pragma: no cover - core dep, guarded anyway
            raise FetchError(
                "curl_cffi is not installed; install scrapesmith's core dependencies",
                url=url,
                strategy_used=self.name,
            ) from exc

        profile = self.select_profile(options)
        target = self._impersonate_target(options)

        # curl_cffi sets its own browser-matched header order for the impersonated
        # target; we only add what the caller asked for plus Accept tuning, and
        # let the library own the rest.
        headers = build_headers(
            profile,
            expected_content_type=self.expected_type(options),
            is_navigation=not options.has_body(),
            extra=options.headers,
        )

        proxies = None
        if options.proxy:
            proxies = {"http": options.proxy, "https": options.proxy}

        started = time.monotonic()
        try:
            async with curl_requests.AsyncSession() as session:
                response = await session.request(
                    options.method,
                    url,
                    params=options.params,
                    data=options.data,
                    json=options.json_body,
                    headers=headers,
                    cookies=options.cookies or None,
                    timeout=options.timeout,
                    allow_redirects=bool(options.follow_redirects),
                    verify=bool(options.verify_ssl),
                    proxies=proxies,
                    impersonate=target,
                )
        except Exception as exc:  # curl_cffi raises its own error hierarchy
            message = str(exc).lower()
            if "proxy" in message:
                raise ProxyError(
                    f"Proxy connection failed: {type(exc).__name__}",
                    url=url,
                    strategy_used=self.name,
                ) from exc
            raise FetchError(
                f"curl_cffi request failed: {type(exc).__name__}: {exc}",
                url=url,
                strategy_used=self.name,
            ) from exc

        encoding = getattr(response, "encoding", None) or "utf-8"
        return self.build_response(
            url=str(getattr(response, "url", url)),
            request_url=url,
            status_code=response.status_code,
            headers=dict(response.headers),
            content=response.content or b"",
            started=started,
            encoding=encoding,
        )
