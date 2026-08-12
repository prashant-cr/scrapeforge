"""Scraper: config merging, robots, rate limiting, and the public API surface."""

from __future__ import annotations

import asyncio

import httpx
import pytest
import respx

from scrapesmith import Scraper
from scrapesmith.config import FetchOptions, ScraperConfig
from scrapesmith.exceptions import ConfigError
from scrapesmith.models import ContentType

URL = "https://example.com/page"


@pytest.fixture
def scraper():
    return Scraper(
        strategies=["http"],
        respect_robots=False,
        min_delay=0.0,
        max_delay=0.0,
        max_retries=0,
        llm_api_key="test-key",
    )


class TestConstruction:
    def test_keyword_overrides(self):
        s = Scraper(timeout=5, strategies=["http"])
        assert s.config.timeout == 5

    def test_prebuilt_config(self):
        cfg = ScraperConfig(timeout=7)
        assert Scraper(cfg).config.timeout == 7

    def test_config_and_overrides_together_is_rejected(self):
        with pytest.raises(ConfigError, match="not both"):
            Scraper(ScraperConfig(), timeout=5)

    def test_invalid_field_is_a_config_error(self):
        with pytest.raises(ConfigError, match="Invalid configuration"):
            Scraper(timeout=-1)

    def test_unknown_field_is_a_config_error(self):
        with pytest.raises(ConfigError):
            Scraper(nonexistent_option=True)

    def test_unknown_strategy_is_a_config_error(self):
        with pytest.raises(ConfigError, match="Unknown strategy"):
            Scraper(strategies=["telepathy"])


class TestOptionMerging:
    def test_per_request_overrides_config(self):
        cfg = ScraperConfig(timeout=30, proxy="http://a:1")
        resolved = FetchOptions(timeout=5).resolve(cfg)
        assert resolved.timeout == 5
        assert resolved.proxy == "http://a:1"  # untouched fields inherit

    def test_headers_and_cookies_merge_rather_than_replace(self):
        cfg = ScraperConfig(headers={"A": "1", "B": "2"}, cookies={"x": "1"})
        resolved = FetchOptions(headers={"B": "override"}, cookies={"y": "2"}).resolve(cfg)
        assert resolved.headers == {"A": "1", "B": "override"}
        assert resolved.cookies == {"x": "1", "y": "2"}

    def test_json_alias_is_accepted(self):
        assert FetchOptions(json={"q": 1}).json_body == {"q": 1}

    def test_method_is_normalized(self):
        assert FetchOptions(method="post").method == "POST"

    def test_unknown_option_is_rejected(self, scraper):
        with pytest.raises(ConfigError, match="Unknown fetch option"):
            scraper._build_options({"tiemout": 5})


class TestFetch:
    @respx.mock
    async def test_fetch_returns_a_response(self, scraper, product_html):
        respx.get(URL).mock(return_value=httpx.Response(200, html=product_html))

        response = await scraper.fetch(URL)

        assert response.status_code == 200
        assert response.strategy_used == "http"
        assert response.content_type is ContentType.HTML

    @respx.mock
    async def test_post_with_json(self, scraper):
        route = respx.post(URL).mock(return_value=httpx.Response(200, json={"ok": True}))

        response = await scraper.fetch(URL, method="POST", json={"q": "shoes"})

        assert route.calls[0].request.content == b'{"q":"shoes"}'
        assert response.json() == {"ok": True}

    @respx.mock
    async def test_per_request_proxy_does_not_mutate_config(self, scraper):
        respx.get(URL).mock(return_value=httpx.Response(200, text="ok"))

        await scraper.fetch(URL, proxy="http://other:8080")

        assert scraper.config.proxy is None


class TestRobots:
    @respx.mock
    async def test_disallowed_url_is_refused(self):
        respx.get("https://example.com/robots.txt").mock(
            return_value=httpx.Response(200, text="User-agent: *\nDisallow: /private")
        )
        s = Scraper(strategies=["http"], respect_robots=True, min_delay=0, max_delay=0)

        with pytest.raises(ConfigError, match=r"robots\.txt disallows"):
            await s.fetch("https://example.com/private/data")

    @respx.mock
    async def test_allowed_url_proceeds(self):
        respx.get("https://example.com/robots.txt").mock(
            return_value=httpx.Response(200, text="User-agent: *\nDisallow: /private")
        )
        respx.get("https://example.com/public").mock(return_value=httpx.Response(200, text="ok"))
        s = Scraper(strategies=["http"], respect_robots=True, min_delay=0, max_delay=0)

        assert (await s.fetch("https://example.com/public")).status_code == 200

    @respx.mock
    async def test_missing_robots_is_treated_as_allowed(self):
        respx.get("https://example.com/robots.txt").mock(return_value=httpx.Response(404))
        respx.get(URL).mock(return_value=httpx.Response(200, text="ok"))
        s = Scraper(strategies=["http"], respect_robots=True, min_delay=0, max_delay=0)

        assert (await s.fetch(URL)).status_code == 200

    @respx.mock
    async def test_unreachable_robots_is_treated_as_allowed(self):
        respx.get("https://example.com/robots.txt").mock(side_effect=httpx.ConnectError("down"))
        respx.get(URL).mock(return_value=httpx.Response(200, text="ok"))
        s = Scraper(strategies=["http"], respect_robots=True, min_delay=0, max_delay=0)

        assert (await s.fetch(URL)).status_code == 200

    @respx.mock
    async def test_opting_out_skips_the_check_entirely(self):
        robots = respx.get("https://example.com/robots.txt").mock(
            return_value=httpx.Response(200, text="User-agent: *\nDisallow: /")
        )
        respx.get(URL).mock(return_value=httpx.Response(200, text="ok"))
        s = Scraper(strategies=["http"], respect_robots=False, min_delay=0, max_delay=0)

        await s.fetch(URL)

        assert robots.call_count == 0

    @respx.mock
    async def test_robots_is_fetched_once_per_origin(self):
        robots = respx.get("https://example.com/robots.txt").mock(
            return_value=httpx.Response(200, text="User-agent: *\nAllow: /")
        )
        respx.get(URL).mock(return_value=httpx.Response(200, text="ok"))
        s = Scraper(strategies=["http"], respect_robots=True, min_delay=0, max_delay=0)

        await asyncio.gather(*(s.fetch(URL) for _ in range(5)))

        assert robots.call_count == 1


class TestRateLimiting:
    @respx.mock
    async def test_per_domain_concurrency_is_capped(self):
        in_flight = 0
        peak = 0

        async def handler(request):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.02)
            in_flight -= 1
            return httpx.Response(200, text="ok")

        respx.get(URL).mock(side_effect=handler)
        s = Scraper(
            strategies=["http"],
            respect_robots=False,
            max_concurrency_per_domain=2,
            min_delay=0,
            max_delay=0,
        )

        await asyncio.gather(*(s.fetch(URL) for _ in range(8)))

        assert peak <= 2

    @respx.mock
    async def test_delay_is_enforced_between_requests(self):
        respx.get(URL).mock(return_value=httpx.Response(200, text="ok"))
        s = Scraper(
            strategies=["http"],
            respect_robots=False,
            max_concurrency_per_domain=1,
            min_delay=0.05,
            max_delay=0.05,
        )

        start = asyncio.get_running_loop().time()
        for _ in range(3):
            await s.fetch(URL)
        elapsed = asyncio.get_running_loop().time() - start

        assert elapsed >= 0.10

    @respx.mock
    async def test_limits_are_per_host_not_global(self):
        respx.get("https://a.example/x").mock(return_value=httpx.Response(200, text="a"))
        respx.get("https://b.example/x").mock(return_value=httpx.Response(200, text="b"))
        s = Scraper(
            strategies=["http"],
            respect_robots=False,
            max_concurrency_per_domain=1,
            min_delay=0.05,
            max_delay=0.05,
        )

        start = asyncio.get_running_loop().time()
        await asyncio.gather(s.fetch("https://a.example/x"), s.fetch("https://b.example/x"))
        elapsed = asyncio.get_running_loop().time() - start

        assert elapsed < 0.05, "different hosts must not queue behind each other"


class TestExtract:
    async def test_extract_from_a_string_with_selectors(
        self, scraper, product_html, product_schema
    ):
        product = await scraper.extract(
            product_html,
            product_schema,
            parser="css",
            selectors={"name": "h1.title", "price": ".price@data-amount"},
        )
        assert product.name == "Trail Runner GTX"

    @respx.mock
    async def test_extract_from_a_response_reuses_detected_type(
        self, scraper, product_json, product_schema
    ):
        respx.get(URL).mock(
            return_value=httpx.Response(
                200, text=product_json, headers={"content-type": "application/json"}
            )
        )
        response = await scraper.fetch(URL)

        product = await scraper.extract(
            response,
            product_schema,
            parser="jsonpath",
            selectors={"name": "product.title", "price": "product.offers.0.price"},
        )

        assert product.name == "Trail Runner GTX"

    async def test_content_type_override(self, scraper, product_json, product_schema):
        product = await scraper.extract(
            product_json,
            product_schema,
            content_type="json",
            parser="jsonpath",
            selectors={"name": "product.title", "price": "product.offers.0.price"},
        )
        assert product.price == 149.95

    async def test_selector_parser_without_selectors_is_rejected(self, scraper, product_schema):
        with pytest.raises(ConfigError, match="requires a 'selectors' mapping"):
            await scraper.extract("<html></html>", product_schema, parser="css")

    async def test_unknown_parser_is_rejected(self, scraper, product_schema):
        with pytest.raises(ConfigError, match="Unknown parser"):
            await scraper.extract("<html></html>", product_schema, parser="magic")


class TestScrape:
    @respx.mock
    async def test_scrape_fetches_then_extracts(self, scraper, product_html, product_schema):
        respx.get(URL).mock(return_value=httpx.Response(200, html=product_html))

        product = await scraper.scrape(
            URL,
            product_schema,
            parser="css",
            selectors={"name": "h1.title", "price": ".price@data-amount"},
        )

        assert product.name == "Trail Runner GTX"
        assert product.price == 149.95

    @respx.mock
    async def test_fetch_options_pass_through_scrape(self, scraper, product_html, product_schema):
        route = respx.get(URL).mock(return_value=httpx.Response(200, html=product_html))

        await scraper.scrape(
            URL,
            product_schema,
            parser="css",
            selectors={"name": "h1.title", "price": ".price@data-amount"},
            headers={"X-Trace": "1"},
        )

        assert route.calls[0].request.headers["x-trace"] == "1"


class TestSyncMirrors:
    def test_fetch_sync(self, product_html):
        s = Scraper(strategies=["http"], respect_robots=False, min_delay=0, max_delay=0)
        with respx.mock:
            respx.get(URL).mock(return_value=httpx.Response(200, html=product_html))
            response = s.fetch_sync(URL)
        assert response.status_code == 200

    def test_extract_sync(self, product_html, product_schema):
        s = Scraper(strategies=["http"], respect_robots=False)
        product = s.extract_sync(
            product_html,
            product_schema,
            parser="css",
            selectors={"name": "h1.title", "price": ".price@data-amount"},
        )
        assert product.name == "Trail Runner GTX"

    async def test_sync_inside_a_running_loop_is_refused(self, scraper):
        with pytest.raises(ConfigError, match="running event loop"):
            scraper.fetch_sync(URL)


class TestLifecycle:
    async def test_async_context_manager_closes(self, product_html):
        with respx.mock:
            respx.get(URL).mock(return_value=httpx.Response(200, html=product_html))
            async with Scraper(
                strategies=["http"], respect_robots=False, min_delay=0, max_delay=0
            ) as s:
                assert (await s.fetch(URL)).status_code == 200

    async def test_aclose_is_idempotent(self, scraper):
        await scraper.aclose()
        await scraper.aclose()
