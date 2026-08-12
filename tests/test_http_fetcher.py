"""HttpxFetcher: header/UA wiring, method and body handling, proxy plumbing."""

from __future__ import annotations

import httpx
import pytest
import respx

from scrapekit.config import FetchOptions, ScraperConfig
from scrapekit.exceptions import FetchError, ProxyError
from scrapekit.fetchers.http import HttpxFetcher
from scrapekit.models import ContentType

URL = "https://example.com/page"


def make_options(config: ScraperConfig, **kwargs) -> FetchOptions:
    return FetchOptions(**kwargs).resolve(config)


@pytest.fixture
def fetcher(config: ScraperConfig) -> HttpxFetcher:
    return HttpxFetcher(config)


@respx.mock
async def test_get_returns_normalized_response(fetcher, config, product_html):
    respx.get(URL).mock(
        return_value=httpx.Response(200, html=product_html, headers={"X-Origin": "edge-1"})
    )

    response = await fetcher.fetch(URL, make_options(config))

    assert response.status_code == 200
    assert response.ok is True
    assert response.content_type is ContentType.HTML
    assert response.strategy_used == "http"
    assert "Trail Runner GTX" in response.text
    assert response.headers["x-origin"] == "edge-1"  # headers are lower-cased
    assert response.request_url == URL


@respx.mock
async def test_sends_a_full_browser_header_set(fetcher, config):
    route = respx.get(URL).mock(return_value=httpx.Response(200, text="ok"))

    await fetcher.fetch(URL, make_options(config))

    sent = route.calls[0].request.headers
    assert sent["user-agent"]
    assert sent["accept"]
    assert sent["accept-language"]
    assert sent["sec-fetch-mode"] == "navigate"


@respx.mock
async def test_rotates_the_user_agent_across_requests(config):
    respx.get(URL).mock(return_value=httpx.Response(200, text="ok"))
    route = respx.routes[0]
    fetcher = HttpxFetcher(config.model_copy(update={"rotate_user_agent": True}))

    for _ in range(30):
        await fetcher.fetch(URL, make_options(config))

    agents = {call.request.headers["user-agent"] for call in route.calls}
    assert len(agents) > 1


@respx.mock
async def test_pinned_user_agent_is_respected(fetcher, config):
    route = respx.get(URL).mock(return_value=httpx.Response(200, text="ok"))

    options = make_options(config, headers={"User-Agent": "my-crawler/2.0"})
    await fetcher.fetch(URL, options)

    assert route.calls[0].request.headers["user-agent"] == "my-crawler/2.0"


@respx.mock
async def test_config_and_per_request_headers_are_merged(config):
    route = respx.get(URL).mock(return_value=httpx.Response(200, text="ok"))
    scoped = config.model_copy(update={"headers": {"X-Client": "scrapekit", "X-Env": "prod"}})
    fetcher = HttpxFetcher(scoped)

    options = FetchOptions(headers={"X-Env": "staging"}).resolve(scoped)
    await fetcher.fetch(URL, options)

    sent = route.calls[0].request.headers
    assert sent["x-client"] == "scrapekit"  # kept from config
    assert sent["x-env"] == "staging"  # per-request wins


@respx.mock
async def test_post_json_body(fetcher, config):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json={"ok": True}))

    options = make_options(config, method="POST", json={"q": "shoes"})
    response = await fetcher.fetch(URL, options)

    assert route.calls[0].request.content == b'{"q":"shoes"}'
    assert response.content_type is ContentType.JSON
    assert response.json() == {"ok": True}


@respx.mock
async def test_post_form_body(fetcher, config):
    route = respx.post(URL).mock(return_value=httpx.Response(200, text="ok"))

    await fetcher.fetch(URL, make_options(config, method="POST", data={"q": "shoes"}))

    assert route.calls[0].request.content == b"q=shoes"


@respx.mock
async def test_post_raw_body(fetcher, config):
    route = respx.post(URL).mock(return_value=httpx.Response(200, text="ok"))

    await fetcher.fetch(URL, make_options(config, method="POST", data="raw-payload"))

    assert route.calls[0].request.content == b"raw-payload"


@respx.mock
async def test_query_params_are_sent(fetcher, config):
    route = respx.get(URL).mock(return_value=httpx.Response(200, text="ok"))

    await fetcher.fetch(URL, make_options(config, params={"page": 2}))

    assert route.calls[0].request.url.params["page"] == "2"


@respx.mock
async def test_cookies_are_sent(fetcher, config):
    route = respx.get(URL).mock(return_value=httpx.Response(200, text="ok"))

    await fetcher.fetch(URL, make_options(config, cookies={"session": "abc"}))

    assert "session=abc" in route.calls[0].request.headers["cookie"]


@respx.mock
async def test_expected_json_tunes_the_accept_header(fetcher, config):
    route = respx.get(URL).mock(return_value=httpx.Response(200, json={}))

    await fetcher.fetch(URL, make_options(config, expected_content_type=ContentType.JSON))

    assert route.calls[0].request.headers["accept"] == "application/json, text/plain, */*"


@respx.mock
async def test_redirects_are_followed_and_final_url_reported(fetcher, config):
    respx.get(URL).mock(
        return_value=httpx.Response(302, headers={"Location": "https://example.com/final"})
    )
    respx.get("https://example.com/final").mock(return_value=httpx.Response(200, text="done"))

    response = await fetcher.fetch(URL, make_options(config))

    assert response.url == "https://example.com/final"
    assert response.request_url == URL


@respx.mock
async def test_transport_error_is_wrapped_in_fetcherror(fetcher, config):
    respx.get(URL).mock(side_effect=httpx.ConnectTimeout("timed out"))

    with pytest.raises(FetchError) as exc_info:
        await fetcher.fetch(URL, make_options(config))

    assert exc_info.value.strategy_used == "http"
    assert exc_info.value.url == URL


@respx.mock
async def test_proxy_error_is_wrapped_in_proxyerror(fetcher, config):
    respx.get(URL).mock(side_effect=httpx.ProxyError("bad proxy"))

    with pytest.raises(ProxyError):
        await fetcher.fetch(URL, make_options(config))


@respx.mock
async def test_non_2xx_is_returned_not_raised(fetcher, config):
    """Judging a response is the chain's job, not the fetcher's."""
    respx.get(URL).mock(return_value=httpx.Response(403, text="denied"))

    response = await fetcher.fetch(URL, make_options(config))

    assert response.status_code == 403
    assert response.ok is False
