"""A guided walkthrough of scrapeforge against real, public example sites.

This file is meant to be *read*, not just run. Every test is a small, complete
illustration of one capability, asserted against a live site rather than a
fixture — so it shows what the library actually does in the wild, and fails
loudly if reality drifts from the documentation.

Why these sites, specifically:

* ``example.com`` — reserved by IANA for use in documentation. The canonical
  "safe to request" URL.
* ``books.toscrape.com`` / ``quotes.toscrape.com`` — sandboxes published
  expressly so people can practise scraping. Using them is their intended
  purpose, not a grey area.
* ``postman-echo.com`` — a request/response echo service built for testing,
  which makes it the honest way to prove what we actually put on the wire.

Nothing here scrapes a site that did not invite it, and the library's politeness
controls stay on: ``robots.txt`` is honored and requests to a host are spaced.

These tests are marked ``network``. Run just them with::

    pytest -m network

Skip them (what CI's blocking jobs do) with::

    pytest -m "not network"
"""

from __future__ import annotations

import re

import pytest
from pydantic import BaseModel, Field, field_validator

from scrapeforge import ChallengeError, ContentType, Scraper

from .helpers import requires_browser

# Every test in this module reaches a third-party host.
pytestmark = pytest.mark.network


# --- The links this file uses -------------------------------------------------

SIMPLE_PAGE = "https://example.com/"
BOOK_PAGE = "https://books.toscrape.com/catalogue/a-light-in-the-attic_1000/index.html"
QUOTES_PAGE = "https://quotes.toscrape.com/"
QUOTES_JS_PAGE = "https://quotes.toscrape.com/js/"  # same data, built by JavaScript
ECHO_GET = "https://postman-echo.com/get"
ECHO_POST = "https://postman-echo.com/post"
BLOCKED_ENDPOINT = "https://postman-echo.com/status/403"


@pytest.fixture
def scraper() -> Scraper:
    """A politely-configured scraper, as you would actually use one.

    The rate limiter is left on with a small delay rather than disabled: these
    tests hit somebody else's servers, and the library's defaults exist to be
    used, not switched off the moment they are inconvenient.
    """
    return Scraper(
        strategies=["http", "impersonate"],
        respect_robots=True,
        min_delay=0.2,
        max_delay=0.5,
        timeout=45,
    )


# --- The schema a caller would write ------------------------------------------


class Book(BaseModel):
    """A book listing, as it appears on books.toscrape.com."""

    title: str
    price: float = Field(description="Numeric price, without a currency symbol")
    currency: str = "GBP"
    in_stock: bool = False
    upc: str | None = Field(default=None, description="Unique product code")

    @field_validator("price", mode="before")
    @classmethod
    def _strip_currency_symbol(cls, value: object) -> object:
        # Selectors hand back raw page text — "£51.77". Coercion belongs in the
        # schema, which is the one place that knows what the field means. (An
        # LLM parser would normally do this for you; a CSS selector will not.)
        if isinstance(value, str):
            return re.sub(r"[^\d.]", "", value) or value
        return value

    @field_validator("in_stock", mode="before")
    @classmethod
    def _availability_text_to_bool(cls, value: object) -> object:
        if isinstance(value, str):
            return "in stock" in value.lower()
        return value


class QuotePage(BaseModel):
    """Several quotes at once — list fields are filled by the ``[]`` suffix."""

    quotes: list[str]
    authors: list[str]
    tags: list[str]


# --- 1. Fetching --------------------------------------------------------------


class TestFetching:
    async def test_fetch_a_simple_page(self, scraper):
        """The smallest useful thing: a URL in, a normalized response out."""
        async with scraper:
            response = await scraper.fetch(SIMPLE_PAGE)

        assert response.status_code == 200
        assert response.content_type is ContentType.HTML
        assert "Example Domain" in response.text
        # The final URL after redirects, which may differ from what you asked for.
        assert response.url.startswith("https://example.com")

    async def test_the_response_says_which_strategy_answered(self, scraper):
        """`strategy_used` is how you find out how much the page cost you."""
        async with scraper:
            response = await scraper.fetch(SIMPLE_PAGE)

        # The chain starts at the cheapest rung and only escalates when blocked,
        # so an unprotected page should be answered by plain HTTP.
        assert response.strategy_used == "http"
        assert response.elapsed > 0

    async def test_forcing_a_specific_strategy(self, scraper):
        """Any strategy can be pinned per request, bypassing the chain."""
        async with scraper:
            response = await scraper.fetch(SIMPLE_PAGE, strategies=["impersonate"])

        # curl_cffi replays a real browser's TLS handshake rather than Python's.
        assert response.strategy_used == "impersonate"
        assert "Example Domain" in response.text

    async def test_robots_txt_is_consulted_and_permits(self, scraper):
        """`respect_robots=True` is on by default and checked before every fetch.

        This asserts the allow path against a live policy. The refusal path is
        covered hermetically in ``test_client.py`` — pinning a real third party's
        ``Disallow`` rules here would make the test rot the day they edit them.
        """
        async with scraper:
            response = await scraper.fetch(QUOTES_PAGE)

        assert response.status_code == 200
        assert scraper.config.respect_robots is True


# --- 2. JSON APIs -------------------------------------------------------------


class TestJsonApis:
    async def test_get_json(self, scraper):
        async with scraper:
            response = await scraper.fetch(ECHO_GET, params={"q": "shoes", "page": 2})

        assert response.content_type is ContentType.JSON
        assert response.json()["args"] == {"q": "shoes", "page": "2"}

    async def test_post_json_body(self, scraper):
        async with scraper:
            response = await scraper.fetch(
                ECHO_POST,
                method="POST",
                json={"q": "shoes", "size": 42},
                headers={"X-Scrapeforge-Example": "1"},
            )

        payload = response.json()
        assert payload["data"] == {"q": "shoes", "size": 42}
        # Header casing varies by server; HTTP headers are case-insensitive.
        seen = {k.lower(): v for k, v in payload["headers"].items()}
        assert seen["x-scrapeforge-example"] == "1"

    async def test_the_headers_we_actually_send_look_like_a_browser(self, scraper):
        """Proof, from the far end, that the fingerprint work reaches the wire."""
        async with scraper:
            response = await scraper.fetch(ECHO_GET)

        seen = {k.lower(): v for k, v in response.json()["headers"].items()}
        assert "mozilla/5.0" in seen["user-agent"].lower()
        assert seen["accept"]
        assert seen["accept-language"]
        # We only advertise encodings we can actually decode — otherwise the
        # body comes back as compressed bytes.
        assert "gzip" in seen["accept-encoding"]


# --- 3. Extraction into your schema -------------------------------------------


class TestExtraction:
    async def test_extract_a_product_with_css_selectors(self, scraper):
        """Fetch and fill a schema in one call — no LLM, no API key, no tokens.

        The selector syntax: plain text by default, ``@attr`` for an attribute,
        and a trailing ``[]`` to collect every match into a list.
        """
        async with scraper:
            book = await scraper.scrape(
                BOOK_PAGE,
                Book,
                parser="css",
                selectors={
                    "title": "h1",
                    "price": ".price_color",
                    "in_stock": ".instock.availability",
                    "upc": "table.table-striped td",
                },
            )

        assert book.title == "A Light in the Attic"
        assert book.price == pytest.approx(51.77)
        assert book.in_stock is True
        assert book.upc == "a897fe39b1053632"
        assert book.currency == "GBP"  # schema default, nothing on the page

    async def test_extract_several_records_with_list_selectors(self, scraper):
        async with scraper:
            page = await scraper.scrape(
                QUOTES_PAGE,
                QuotePage,
                parser="css",
                selectors={
                    "quotes": ".quote .text[]",
                    "authors": ".quote .author[]",
                    "tags": ".quote a.tag[]",
                },
            )

        assert len(page.quotes) == 10
        assert len(page.authors) == 10
        assert "Albert Einstein" in page.authors
        assert any("world as we have created it" in q for q in page.quotes)
        assert "thinking" in page.tags

    async def test_fetch_once_then_extract_without_touching_the_network(self, scraper):
        """`extract` works on content you already have — useful when you want to
        fill several schemas from one page, or re-parse without re-fetching."""
        async with scraper:
            response = await scraper.fetch(BOOK_PAGE)

            book = await scraper.extract(
                response,  # a FetchResponse carries its own detected content type
                Book,
                parser="css",
                selectors={"title": "h1", "price": ".price_color"},
            )

        assert book.title == "A Light in the Attic"
        assert book.price == pytest.approx(51.77)


# --- 4. When the page needs JavaScript ----------------------------------------


@requires_browser
class TestJavaScriptRendering:
    """``/js/`` serves the same quotes, but builds them in the browser.

    This is the case the browser rung exists for, and it doubles as a regression
    test: context-wide request headers once broke subresource loading here, so
    the page fetched fine and rendered nothing.
    """

    @staticmethod
    def _count_quotes(html: str) -> int:
        from selectolax.parser import HTMLParser

        return len(HTMLParser(html).css(".quote .text"))

    async def test_plain_http_sees_no_rendered_quotes(self, scraper):
        async with scraper:
            response = await scraper.fetch(QUOTES_JS_PAGE, strategies=["http"])

        assert response.status_code == 200
        # The data is in the page as a JS array, but no .quote element exists yet.
        assert self._count_quotes(response.text) == 0

    async def test_the_browser_renders_them(self):
        async with Scraper(
            strategies=["browser"], respect_robots=True, min_delay=0.2, max_delay=0.5, timeout=60
        ) as browser_scraper:
            response = await browser_scraper.fetch(QUOTES_JS_PAGE, wait_until="networkidle")

        assert response.strategy_used == "browser"
        assert self._count_quotes(response.text) == 10

    async def test_selectors_work_against_the_rendered_dom(self):
        """The payoff: the same selectors that failed over HTTP now succeed."""
        async with Scraper(
            strategies=["browser"], respect_robots=True, min_delay=0.2, max_delay=0.5, timeout=60
        ) as browser_scraper:
            page = await browser_scraper.scrape(
                QUOTES_JS_PAGE,
                QuotePage,
                parser="css",
                selectors={
                    "quotes": ".quote .text[]",
                    "authors": ".quote .author[]",
                    "tags": ".quote a.tag[]",
                },
                wait_until="networkidle",
            )

        assert len(page.quotes) == 10
        assert "Albert Einstein" in page.authors


# --- 5. When you get blocked --------------------------------------------------


class TestBeingBlocked:
    async def test_a_block_raises_challenge_error_rather_than_returning_junk(self, scraper):
        """A 403 with a stub body is treated as a block, not as content.

        Every strategy is tried first; when they all come back blocked you get a
        ``ChallengeError`` describing what matched. scrapeforge deliberately stops
        here rather than trying to defeat the challenge — the decision (a
        different proxy, your own solver, backing off) is yours.
        """
        async with scraper:
            with pytest.raises(ChallengeError) as exc_info:
                await scraper.fetch(BLOCKED_ENDPOINT)

        error = exc_info.value
        assert error.signature, "the matched heuristic should be reported"
        assert error.status_code == 403
