"""TlsClientFetcher.

``tls-client`` is a synchronous, non-mockable-over-HTTP library (it drives its own
Go TLS stack, so ``respx`` cannot see its traffic). These tests substitute a fake
``tls_client`` module instead, which also means they run identically whether or
not the optional extra is installed.

What is worth asserting here is the seam: the parameters we hand the library, the
shape we normalize its response back into, and the errors we translate.
"""

from __future__ import annotations

import asyncio
import importlib.machinery
import sys
import threading
import types
from typing import Any

import pytest

from scrapeforge.config import FetchOptions, ScraperConfig
from scrapeforge.exceptions import ConfigError, FetchError, ProxyError
from scrapeforge.fetchers.tls import TlsClientFetcher
from scrapeforge.models import ContentType

URL = "https://example.com/page"


class FakeResponse:
    """Mimics a ``tls_client`` response: text (not bytes), and no encoding."""

    def __init__(
        self,
        *,
        status_code: int | None = 200,
        text: str = "ok",
        headers: dict[str, str] | None = None,
        url: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.text = text
        self.headers = headers if headers is not None else {"Content-Type": "text/html"}
        self.url = url or URL


class FakeSession:
    """Records how it was constructed and called, then returns a canned response."""

    #: Populated by the fixture so tests can inspect the last session built.
    last: FakeSession | None = None

    def __init__(self, **init_kwargs: Any) -> None:
        self.init_kwargs = init_kwargs
        self.request_kwargs: dict[str, Any] = {}
        self.response: Any = FakeResponse()
        self.error: Exception | None = None
        self.on_request = None
        self.closed = 0
        FakeSession.last = self

    def execute_request(self, **kwargs: Any) -> Any:
        self.request_kwargs = kwargs
        if self.on_request is not None:
            self.on_request(self)
        if self.error is not None:
            raise self.error
        return self.response

    def close(self) -> str:
        self.closed += 1
        return "{}"


@pytest.fixture
def fake_tls(monkeypatch: pytest.MonkeyPatch):
    """Install a fake ``tls_client`` module for the duration of a test.

    ``__spec__`` must be set: ``importlib.util.find_spec`` reads it off the
    cached module, and a bare ``ModuleType`` would make ``is_available()`` report
    the dependency as missing.
    """
    module = types.ModuleType("tls_client")
    module.__spec__ = importlib.machinery.ModuleSpec("tls_client", loader=None)

    # ``sessions`` records every session built, which is race-free under the
    # concurrency tests in a way that reading ``FakeSession.last`` is not.
    state: dict[str, Any] = {
        "response": FakeResponse(),
        "error": None,
        "on_request": None,
        "close_error": None,
        "sessions": [],
    }

    def session_factory(**init_kwargs: Any) -> FakeSession:
        session = FakeSession(**init_kwargs)
        session.response = state["response"]
        session.error = state["error"]
        session.on_request = state["on_request"]
        if state["close_error"] is not None:
            error = state["close_error"]

            def failing_close() -> str:
                session.closed += 1
                raise error

            session.close = failing_close
        state["sessions"].append(session)
        return session

    module.Session = session_factory
    monkeypatch.setitem(sys.modules, "tls_client", module)
    FakeSession.last = None
    return state


@pytest.fixture
def config() -> ScraperConfig:
    return ScraperConfig(
        strategies=["tls"],
        respect_robots=False,
        min_delay=0.0,
        max_delay=0.0,
        max_retries=0,
    )


@pytest.fixture
def fetcher(config: ScraperConfig) -> TlsClientFetcher:
    return TlsClientFetcher(config)


def make_options(config: ScraperConfig, **kwargs: Any) -> FetchOptions:
    return FetchOptions(**kwargs).resolve(config)


class TestAvailability:
    def test_available_when_the_module_is_importable(self, fetcher, fake_tls):
        assert fetcher.is_available() is True

    def test_declares_its_dependency_and_install_hint(self, fetcher):
        assert fetcher.requires == "tls_client"
        assert fetcher.extra_name == "scrapeforge[tls]"

    async def test_missing_dependency_raises_config_error_with_a_fix(
        self, fetcher, config, monkeypatch
    ):
        monkeypatch.setattr("scrapeforge.fetchers.base.module_available", lambda name: False)

        with pytest.raises(ConfigError) as exc_info:
            await fetcher.fetch(URL, make_options(config))

        assert "scrapeforge[tls]" in str(exc_info.value)


class TestSessionConstruction:
    async def test_uses_the_configured_client_identifier(self, config, fake_tls):
        scoped = config.model_copy(update={"tls_client_identifier": "firefox_120"})
        await TlsClientFetcher(scoped).fetch(URL, make_options(scoped))

        assert FakeSession.last.init_kwargs["client_identifier"] == "firefox_120"

    async def test_randomizes_tls_extension_order(self, fetcher, config, fake_tls):
        """A fixed extension order is itself a fingerprint."""
        await fetcher.fetch(URL, make_options(config))

        assert FakeSession.last.init_kwargs["random_tls_extension_order"] is True


class TestRequestWiring:
    async def test_get_defaults(self, fetcher, config, fake_tls):
        await fetcher.fetch(URL, make_options(config))

        sent = FakeSession.last.request_kwargs
        assert sent["method"] == "GET"
        assert sent["url"] == URL
        assert sent["data"] is None
        assert sent["json"] is None
        assert sent["proxy"] is None
        assert sent["allow_redirects"] is True
        assert sent["insecure_skip_verify"] is False

    async def test_sends_a_coherent_browser_header_set(self, fetcher, config, fake_tls):
        await fetcher.fetch(URL, make_options(config))

        headers = FakeSession.last.request_kwargs["headers"]
        assert headers["user-agent"]
        assert headers["accept"]
        assert headers["accept-language"]
        assert headers["sec-fetch-mode"] == "navigate"

    async def test_rotates_the_user_agent(self, config, fake_tls):
        scoped = config.model_copy(update={"rotate_user_agent": True})
        fetcher = TlsClientFetcher(scoped)

        agents = set()
        for _ in range(30):
            await fetcher.fetch(URL, make_options(scoped))
            agents.add(FakeSession.last.request_kwargs["headers"]["user-agent"])

        assert len(agents) > 1

    async def test_pinned_user_agent_is_respected(self, fetcher, config, fake_tls):
        options = make_options(config, headers={"User-Agent": "my-crawler/2.0"})
        await fetcher.fetch(URL, options)

        assert FakeSession.last.request_kwargs["headers"]["user-agent"] == "my-crawler/2.0"

    async def test_config_and_per_request_headers_are_merged(self, config, fake_tls):
        scoped = config.model_copy(update={"headers": {"X-Client": "scrapeforge", "X-Env": "prod"}})
        options = FetchOptions(headers={"X-Env": "staging"}).resolve(scoped)

        await TlsClientFetcher(scoped).fetch(URL, options)

        headers = FakeSession.last.request_kwargs["headers"]
        assert headers["X-Client"] == "scrapeforge"
        assert headers["X-Env"] == "staging"

    async def test_post_json_body(self, fetcher, config, fake_tls):
        await fetcher.fetch(URL, make_options(config, method="POST", json={"q": "shoes"}))

        sent = FakeSession.last.request_kwargs
        assert sent["method"] == "POST"
        assert sent["json"] == {"q": "shoes"}
        assert sent["data"] is None

    async def test_post_form_body(self, fetcher, config, fake_tls):
        await fetcher.fetch(URL, make_options(config, method="POST", data={"q": "shoes"}))

        sent = FakeSession.last.request_kwargs
        assert sent["data"] == {"q": "shoes"}
        assert sent["json"] is None

    async def test_post_raw_string_body(self, fetcher, config, fake_tls):
        await fetcher.fetch(URL, make_options(config, method="POST", data="raw-payload"))

        assert FakeSession.last.request_kwargs["data"] == "raw-payload"

    async def test_post_raw_bytes_body_is_decoded(self, fetcher, config, fake_tls):
        """tls-client takes str, not bytes — bytes must be decoded, not passed through."""
        await fetcher.fetch(URL, make_options(config, method="POST", data=b"raw-bytes"))

        assert FakeSession.last.request_kwargs["data"] == "raw-bytes"

    async def test_params_and_cookies(self, fetcher, config, fake_tls):
        options = make_options(config, params={"page": 2}, cookies={"session": "abc"})
        await fetcher.fetch(URL, options)

        sent = FakeSession.last.request_kwargs
        assert sent["params"] == {"page": 2}
        assert sent["cookies"] == {"session": "abc"}

    async def test_empty_cookies_are_sent_as_none(self, fetcher, config, fake_tls):
        await fetcher.fetch(URL, make_options(config))

        assert FakeSession.last.request_kwargs["cookies"] is None

    async def test_proxy_is_wired_through(self, fetcher, config, fake_tls):
        await fetcher.fetch(URL, make_options(config, proxy="http://user:pass@host:8080"))

        assert FakeSession.last.request_kwargs["proxy"] == "http://user:pass@host:8080"

    async def test_verify_ssl_off_sets_insecure_skip_verify(self, fetcher, config, fake_tls):
        await fetcher.fetch(URL, make_options(config, verify_ssl=False))

        assert FakeSession.last.request_kwargs["insecure_skip_verify"] is True

    async def test_follow_redirects_off(self, fetcher, config, fake_tls):
        await fetcher.fetch(URL, make_options(config, follow_redirects=False))

        assert FakeSession.last.request_kwargs["allow_redirects"] is False

    async def test_timeout_is_coerced_to_whole_seconds(self, fetcher, config, fake_tls):
        """tls-client takes an int; a float would raise inside the library."""
        await fetcher.fetch(URL, make_options(config, timeout=12.7))

        timeout = FakeSession.last.request_kwargs["timeout_seconds"]
        assert isinstance(timeout, int)
        assert timeout == 12

    async def test_json_expectation_tunes_the_accept_header(self, fetcher, config, fake_tls):
        options = make_options(config, expected_content_type=ContentType.JSON)
        await fetcher.fetch(URL, options)

        headers = FakeSession.last.request_kwargs["headers"]
        assert headers["accept"] == "application/json, text/plain, */*"


class TestResponseNormalization:
    async def test_maps_status_headers_and_final_url(self, fetcher, config, fake_tls, product_html):
        fake_tls["response"] = FakeResponse(
            status_code=200,
            text=product_html,
            headers={"Content-Type": "text/html", "X-Origin": "edge-1"},
            url="https://example.com/final",
        )

        response = await fetcher.fetch(URL, make_options(config))

        assert response.status_code == 200
        assert response.ok is True
        assert response.url == "https://example.com/final"
        assert response.request_url == URL
        assert response.headers["x-origin"] == "edge-1"  # normalized to lower case
        assert response.strategy_used == "tls"

    async def test_text_is_encoded_to_bytes(self, fetcher, config, fake_tls):
        fake_tls["response"] = FakeResponse(text="héllo wörld")

        response = await fetcher.fetch(URL, make_options(config))

        assert isinstance(response.content, bytes)
        assert response.text == "héllo wörld"
        assert response.encoding == "utf-8"

    async def test_detects_json_content(self, fetcher, config, fake_tls):
        fake_tls["response"] = FakeResponse(
            text='{"ok": true}', headers={"Content-Type": "application/json"}
        )

        response = await fetcher.fetch(URL, make_options(config))

        assert response.content_type is ContentType.JSON
        assert response.json() == {"ok": True}

    async def test_detects_html_content(self, fetcher, config, fake_tls, product_html):
        fake_tls["response"] = FakeResponse(text=product_html)

        response = await fetcher.fetch(URL, make_options(config))

        assert response.content_type is ContentType.HTML

    async def test_records_elapsed_time(self, fetcher, config, fake_tls):
        response = await fetcher.fetch(URL, make_options(config))

        assert response.elapsed >= 0

    async def test_missing_headers_are_tolerated(self, fetcher, config, fake_tls):
        fake_tls["response"] = FakeResponse(text="plain", headers={})

        response = await fetcher.fetch(URL, make_options(config))

        assert response.headers == {}
        assert response.content_type is ContentType.TEXT

    async def test_empty_body_is_tolerated(self, fetcher, config, fake_tls):
        fake_tls["response"] = FakeResponse(text="")

        response = await fetcher.fetch(URL, make_options(config))

        assert response.content == b""

    async def test_non_2xx_is_returned_not_raised(self, fetcher, config, fake_tls):
        """Judging a response is the chain's job, not the fetcher's."""
        fake_tls["response"] = FakeResponse(status_code=403, text="denied")

        response = await fetcher.fetch(URL, make_options(config))

        assert response.status_code == 403
        assert response.ok is False


class TestErrorTranslation:
    async def test_generic_failure_becomes_fetch_error(self, fetcher, config, fake_tls):
        fake_tls["error"] = RuntimeError("connection reset by peer")

        with pytest.raises(FetchError) as exc_info:
            await fetcher.fetch(URL, make_options(config))

        error = exc_info.value
        assert not isinstance(error, ProxyError)
        assert error.url == URL
        assert error.strategy_used == "tls"
        assert "connection reset" in str(error)

    async def test_proxy_failure_becomes_proxy_error(self, fetcher, config, fake_tls):
        fake_tls["error"] = RuntimeError("failed to connect to proxy host")

        with pytest.raises(ProxyError) as exc_info:
            await fetcher.fetch(URL, make_options(config))

        assert exc_info.value.strategy_used == "tls"

    async def test_proxy_error_does_not_leak_credentials(self, fetcher, config, fake_tls):
        fake_tls["error"] = RuntimeError("proxy rejected the connection")

        with pytest.raises(ProxyError) as exc_info:
            await fetcher.fetch(URL, make_options(config, proxy="http://user:hunter2@host:8080"))

        assert "hunter2" not in str(exc_info.value)

    async def test_library_exceptions_never_escape_the_hierarchy(self, fetcher, config, fake_tls):
        """Callers should never have to catch a tls-client exception."""

        class TlsClientInternalError(Exception):
            pass

        fake_tls["error"] = TlsClientInternalError("something went wrong in the Go layer")

        with pytest.raises(FetchError):
            await fetcher.fetch(URL, make_options(config))

    async def test_missing_status_raises_fetch_error(self, fetcher, config, fake_tls):
        """tls-client returns a response object with a null status on some failures."""
        fake_tls["response"] = FakeResponse(status_code=None, text="")

        with pytest.raises(FetchError, match="no status"):
            await fetcher.fetch(URL, make_options(config))


class TestSessionCleanup:
    """Each Session allocates a session in the library's Go runtime that is only
    reclaimed by an explicit close. Leaking one per request breaks long scrapes."""

    async def test_session_is_closed_after_a_successful_request(self, fetcher, config, fake_tls):
        await fetcher.fetch(URL, make_options(config))

        assert [s.closed for s in fake_tls["sessions"]] == [1]

    async def test_session_is_closed_when_the_request_fails(self, fetcher, config, fake_tls):
        fake_tls["error"] = RuntimeError("connection reset by peer")

        with pytest.raises(FetchError):
            await fetcher.fetch(URL, make_options(config))

        assert [s.closed for s in fake_tls["sessions"]] == [1]

    async def test_a_close_failure_does_not_mask_the_response(self, fetcher, config, fake_tls):
        """Cleanup is best-effort; it must not turn a good response into an error."""
        fake_tls["close_error"] = RuntimeError("destroySession failed")

        response = await fetcher.fetch(URL, make_options(config))

        assert response.status_code == 200

    async def test_a_close_failure_does_not_mask_the_original_error(
        self, fetcher, config, fake_tls
    ):
        fake_tls["error"] = RuntimeError("connection reset by peer")
        fake_tls["close_error"] = RuntimeError("destroySession failed")

        with pytest.raises(FetchError, match="connection reset"):
            await fetcher.fetch(URL, make_options(config))

    async def test_every_request_closes_its_own_session(self, fetcher, config, fake_tls):
        for _ in range(3):
            await fetcher.fetch(URL, make_options(config))

        assert len(fake_tls["sessions"]) == 3
        assert all(s.closed == 1 for s in fake_tls["sessions"])


class TestAsyncBehaviour:
    async def test_blocking_call_runs_in_a_worker_thread(self, fetcher, config, fake_tls):
        """tls-client is synchronous; running it inline would stall the event loop."""
        entered = threading.Event()
        release = threading.Event()
        main_thread = threading.get_ident()
        worker_thread: list[int] = []

        def blocking_request(_session: FakeSession) -> None:
            worker_thread.append(threading.get_ident())
            entered.set()
            # Bounded so a regression fails the test instead of hanging it: if
            # this ran inline, the loop could never reach the release below.
            release.wait(timeout=5)

        fake_tls["on_request"] = blocking_request

        async def observer() -> bool:
            # Only reachable while the blocking call is in flight, which proves
            # the event loop was never stalled by it.
            while not entered.is_set():
                await asyncio.sleep(0)
            release.set()
            return True

        observed, response = await asyncio.gather(
            observer(), fetcher.fetch(URL, make_options(config))
        )

        assert observed is True, "the event loop stalled while tls-client blocked"
        assert worker_thread and worker_thread[0] != main_thread
        assert response.status_code == 200

    async def test_concurrent_requests_each_get_their_own_session(self, fetcher, config, fake_tls):
        """Sessions are per-request, so concurrent calls cannot share Go-side state."""
        await asyncio.gather(*(fetcher.fetch(URL, make_options(config)) for _ in range(5)))

        sessions = fake_tls["sessions"]
        assert len(sessions) == 5
        assert len({id(s) for s in sessions}) == 5
        assert all(s.closed == 1 for s in sessions)
