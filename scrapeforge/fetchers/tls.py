"""Alternate TLS impersonation via ``tls-client``.

Secondary to :class:`~scrapeforge.fetchers.impersonate.CurlCffiFetcher`; useful
when a specific client profile is needed that curl_cffi does not ship. Disabled
by default — add ``"tls"`` to ``strategies`` to enable it.

``tls-client`` is a synchronous library, so requests run in a worker thread to
keep the async API honest.
"""

from __future__ import annotations

import asyncio
import contextlib
import json as _json
import time

from ..config import FetchOptions
from ..exceptions import ConfigError, FetchError, ProxyError
from ..fingerprint import build_headers
from ..models import FetchResponse
from .base import BaseFetcher

__all__ = ["TlsClientFetcher"]


class TlsClientFetcher(BaseFetcher):
    """Fetch using a ``tls-client`` profile.

    The profile is chosen by ``ScraperConfig.tls_client_identifier`` (e.g.
    ``"chrome_120"``, ``"firefox_120"``, ``"safari_16_0"``).
    """

    name = "tls"
    requires = "tls_client"
    extra_name = "scrapeforge[tls]"

    def _request_sync(self, url: str, options: FetchOptions, headers: dict[str, str]):
        import tls_client

        session = tls_client.Session(
            client_identifier=self.config.tls_client_identifier,
            random_tls_extension_order=True,
        )
        proxy = options.proxy or None
        body = options.data
        data = None
        if isinstance(body, (str, bytes)):
            data = body.decode() if isinstance(body, bytes) else body
        elif isinstance(body, dict):
            data = body

        try:
            return session.execute_request(
                method=options.method,
                url=url,
                params=options.params,
                data=data,
                json=options.json_body,
                headers=headers,
                cookies=options.cookies or None,
                proxy=proxy,
                allow_redirects=bool(options.follow_redirects),
                insecure_skip_verify=not options.verify_ssl,
                timeout_seconds=int(options.timeout or 30),
            )
        finally:
            # Each Session allocates a session inside the library's Go runtime
            # that is only reclaimed by an explicit close. Without this, a long
            # scrape leaks one per request. The response is fully materialized by
            # the time execute_request returns, so closing here is safe.
            # A failure to close must not mask the request's own outcome.
            with contextlib.suppress(Exception):
                session.close()

    async def fetch(self, url: str, options: FetchOptions) -> FetchResponse:
        if not self.is_available():
            raise ConfigError(
                "The 'tls' strategy requires the tls-client package. Install scrapeforge[tls]."
            )

        profile = self.select_profile(options)
        headers = build_headers(
            profile,
            expected_content_type=self.expected_type(options),
            is_navigation=not options.has_body(),
            extra=options.headers,
        )

        started = time.monotonic()
        try:
            response = await asyncio.to_thread(self._request_sync, url, options, headers)
        except Exception as exc:
            message = str(exc).lower()
            if "proxy" in message:
                raise ProxyError(
                    f"Proxy connection failed: {type(exc).__name__}",
                    url=url,
                    strategy_used=self.name,
                ) from exc
            raise FetchError(
                f"tls-client request failed: {type(exc).__name__}: {exc}",
                url=url,
                strategy_used=self.name,
            ) from exc

        # tls-client exposes text, not bytes, and does not always set an encoding.
        text = getattr(response, "text", "") or ""
        content = text.encode("utf-8", errors="replace")
        headers_out = dict(getattr(response, "headers", {}) or {})
        status = getattr(response, "status_code", 0)
        if status is None:
            raise FetchError(
                f"tls-client returned no status: {_json.dumps(str(response))[:200]}",
                url=url,
                strategy_used=self.name,
            )

        return self.build_response(
            url=str(getattr(response, "url", url) or url),
            request_url=url,
            status_code=int(status),
            headers=headers_out,
            content=content,
            started=started,
            encoding="utf-8",
        )
