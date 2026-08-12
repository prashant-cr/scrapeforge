"""Headless browser fetching with stealth patches — the last rung.

Slowest and heaviest strategy. Reach for it when the page needs JavaScript to
render, or when the lighter strategies are blocked. The browser is launched
lazily on first use and reused across requests; call
:meth:`PlaywrightFetcher.aclose` (or close the ``Scraper``) to shut it down.
"""

from __future__ import annotations

import asyncio
import random
import time

from ..config import FetchOptions
from ..exceptions import ConfigError, FetchError, ProxyError
from ..fingerprint import UserAgentProfile, profile_for_user_agent
from ..fingerprint.stealth import BROWSER_TYPE_TO_FAMILY, build_init_script, launch_args
from ..fingerprint.user_agents import USER_AGENTS, random_profile
from ..models import FetchResponse
from ..utils.proxy import parse_proxy
from .base import BaseFetcher

__all__ = ["PlaywrightFetcher"]

#: Headers a real browser generates itself, per request type. We must not force
#: our own values for these onto the context — see _context_kwargs.
_BROWSER_MANAGED_HEADERS = frozenset(
    {
        "user-agent",
        "accept",
        "accept-language",
        "accept-encoding",
        "upgrade-insecure-requests",
        "sec-fetch-site",
        "sec-fetch-mode",
        "sec-fetch-user",
        "sec-fetch-dest",
        "sec-ch-ua",
        "sec-ch-ua-mobile",
        "sec-ch-ua-platform",
        "priority",
        "connection",
        "host",
        "content-length",
    }
)


class PlaywrightFetcher(BaseFetcher):
    """Render pages in a stealth-patched headless browser.

    Supports proxies, extra headers, cookie injection, and configurable waiting
    (``networkidle``, a selector, or a fixed delay). Requests with a body are
    issued through the browser's request context rather than a navigation, so
    ``POST`` to a JSON endpoint still travels over the browser's TLS stack.
    """

    name = "browser"
    requires = "playwright"
    extra_name = "scrapekit[browser]"

    def __init__(self, config) -> None:
        super().__init__(config)
        self._playwright = None
        self._browser = None
        self._lock = asyncio.Lock()

    async def _ensure_browser(self, options: FetchOptions):
        """Launch the browser once and reuse it for subsequent requests."""
        if self._browser is not None:
            return self._browser

        async with self._lock:
            if self._browser is not None:
                return self._browser
            try:
                from playwright.async_api import async_playwright
            except ImportError as exc:
                raise ConfigError(
                    "The 'browser' strategy requires Playwright. "
                    "Install scrapekit[browser] and run 'playwright install chromium'."
                ) from exc

            self._playwright = await async_playwright().start()
            engine = getattr(self._playwright, self.config.browser_type)
            kwargs: dict[str, object] = {"headless": self.config.headless}
            if self.config.browser_type == "chromium":
                kwargs["args"] = launch_args()
            try:
                self._browser = await engine.launch(**kwargs)
            except Exception as exc:
                await self._shutdown_playwright()
                raise ConfigError(
                    f"Failed to launch {self.config.browser_type}: {exc}. "
                    "Did you run 'playwright install'?"
                ) from exc
            return self._browser

    async def _shutdown_playwright(self) -> None:
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            finally:
                self._playwright = None

    async def aclose(self) -> None:
        """Close the browser and stop the Playwright driver."""
        if self._browser is not None:
            try:
                await self._browser.close()
            finally:
                self._browser = None
        await self._shutdown_playwright()

    def select_profile(self, options: FetchOptions) -> UserAgentProfile:
        """Pick a profile whose browser family matches the engine we launch.

        Overrides the base implementation because the browser fetcher has an
        extra constraint the HTTP fetchers do not: a Firefox ``User-Agent`` on a
        Chromium engine is trivially detectable from the JS surface alone, so the
        profile pool must be filtered to the launched engine.

        A caller-pinned ``User-Agent`` still wins — user intent beats our
        heuristics, and the caller may have a reason.
        """
        for key, value in (options.headers or {}).items():
            if key.lower() == "user-agent":
                return profile_for_user_agent(value)

        family = BROWSER_TYPE_TO_FAMILY.get(self.config.browser_type, "chrome")
        candidates = [p for p in USER_AGENTS if p.browser == family and not p.mobile]
        if not candidates:  # pragma: no cover - pool always covers the three families
            return random_profile()
        if not self.config.rotate_user_agent:
            return candidates[0]
        return random.choice(candidates)

    def _context_kwargs(self, options: FetchOptions) -> tuple[dict, object]:
        profile = self.select_profile(options)
        width, height = profile.viewport

        # Only the caller's own headers are forced onto the context. Everything
        # in _BROWSER_MANAGED_HEADERS is generated by the browser per request,
        # and correctly: a navigation gets Sec-Fetch-Dest: document while the
        # scripts and stylesheets it pulls get script/style. Playwright applies
        # extra_http_headers to *every* request, so injecting our synthetic
        # navigation set here would stamp "document" on subresources too —
        # a blatant incoherence, and enough to stop JS-heavy pages loading at all.
        caller_headers = {
            key: value
            for key, value in (options.headers or {}).items()
            if key.lower() not in _BROWSER_MANAGED_HEADERS
        }
        accept_language = next(
            (v for k, v in (options.headers or {}).items() if k.lower() == "accept-language"),
            profile.accept_language,
        )

        kwargs: dict[str, object] = {
            "user_agent": profile.user_agent,
            "locale": accept_language.split(",", 1)[0],
            "viewport": {"width": width, "height": height},
            "is_mobile": profile.mobile,
            "has_touch": profile.mobile,
            "device_scale_factor": 3 if profile.mobile else 1,
            "extra_http_headers": caller_headers,
            "ignore_https_errors": not options.verify_ssl,
        }

        proxy = parse_proxy(options.proxy)
        if proxy is not None:
            proxy_conf: dict[str, str] = {"server": proxy.server}
            if proxy.username:
                proxy_conf["username"] = proxy.username
            if proxy.password:
                proxy_conf["password"] = proxy.password
            kwargs["proxy"] = proxy_conf

        return kwargs, profile

    @staticmethod
    def _cookie_params(cookies: dict[str, str], url: str) -> list[dict[str, str]]:
        from urllib.parse import urlsplit

        host = urlsplit(url).hostname or ""
        return [{"name": k, "value": v, "domain": host, "path": "/"} for k, v in cookies.items()]

    async def fetch(self, url: str, options: FetchOptions) -> FetchResponse:
        browser = await self._ensure_browser(options)
        context_kwargs, profile = self._context_kwargs(options)
        timeout_ms = int((options.timeout or 30) * 1000)
        started = time.monotonic()

        context = await browser.new_context(**context_kwargs)
        try:
            await context.add_init_script(build_init_script(profile))
            if options.cookies:
                await context.add_cookies(self._cookie_params(options.cookies, url))

            if options.has_body() or options.method != "GET":
                return await self._fetch_via_request_context(
                    context, url, options, timeout_ms, started
                )
            return await self._fetch_via_navigation(context, url, options, timeout_ms, started)
        except (ConfigError, FetchError):
            raise
        except Exception as exc:
            message = str(exc).lower()
            if "proxy" in message or "err_proxy" in message:
                raise ProxyError(
                    f"Proxy connection failed: {type(exc).__name__}",
                    url=url,
                    strategy_used=self.name,
                ) from exc
            raise FetchError(
                f"Browser fetch failed: {type(exc).__name__}: {exc}",
                url=url,
                strategy_used=self.name,
            ) from exc
        finally:
            await context.close()

    @staticmethod
    async def _navigation_body(page, response, headers: dict[str, str]) -> bytes:
        """Return the response body, preferring the raw bytes for non-HTML.

        ``page.content()`` serializes the DOM, which is what you want for a
        JS-rendered page — but a browser wraps a JSON or XML document in
        ``<html><body><pre>...</pre></body></html>``. Since the chain can escalate
        an API request all the way to the browser, returning that wrapper would
        make ``response.json()`` fail on a perfectly good payload. So: raw bytes
        when the origin says it is not HTML, rendered DOM when it is.
        """
        mime = ""
        for key, value in headers.items():
            if key.lower() == "content-type":
                mime = value.split(";", 1)[0].strip().lower()
                break

        is_html = not mime or mime in ("text/html", "application/xhtml+xml")
        if not is_html and response is not None:
            try:
                return await response.body()
            except Exception:  # body unavailable (e.g. some redirects) — fall through
                pass
        return (await page.content()).encode("utf-8")

    async def _fetch_via_navigation(
        self, context, url: str, options: FetchOptions, timeout_ms: int, started: float
    ) -> FetchResponse:
        page = await context.new_page()
        try:
            response = await page.goto(
                url, wait_until=options.wait_until or "domcontentloaded", timeout=timeout_ms
            )

            if options.wait_for_selector:
                await page.wait_for_selector(options.wait_for_selector, timeout=timeout_ms)
            if options.wait_time:
                await page.wait_for_timeout(options.wait_time * 1000)

            status = response.status if response is not None else 200
            headers = dict(response.headers) if response is not None else {}
            content = await self._navigation_body(page, response, headers)
            final_url = page.url
        finally:
            await page.close()

        return self.build_response(
            url=final_url,
            request_url=url,
            status_code=status,
            headers=headers,
            content=content,
            started=started,
        )

    async def _fetch_via_request_context(
        self, context, url: str, options: FetchOptions, timeout_ms: int, started: float
    ) -> FetchResponse:
        kwargs: dict[str, object] = {"method": options.method, "timeout": timeout_ms}
        if options.params:
            kwargs["params"] = options.params
        if options.json_body is not None:
            kwargs["data"] = options.json_body
        elif options.data is not None:
            if isinstance(options.data, dict):
                kwargs["form"] = options.data
            else:
                kwargs["data"] = options.data

        response = await context.request.fetch(url, **kwargs)
        body = await response.body()
        return self.build_response(
            url=response.url,
            request_url=url,
            status_code=response.status,
            headers=dict(response.headers),
            content=body,
            started=started,
        )
