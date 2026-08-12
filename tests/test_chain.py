"""FallbackChain orchestration: escalation, retries, availability, errors.

Fake fetchers are used throughout so the tests exercise the chain's decision
logic rather than any particular HTTP client.
"""

from __future__ import annotations

import pytest

from scrapeforge.config import FetchOptions, ScraperConfig
from scrapeforge.exceptions import (
    AllStrategiesFailed,
    ChallengeError,
    ConfigError,
    FetchError,
    ProxyError,
)
from scrapeforge.fetchers.base import BaseFetcher
from scrapeforge.fetchers.chain import FETCHER_REGISTRY, FallbackChain, register_fetcher
from scrapeforge.models import ContentType, FetchResponse

URL = "https://example.com/page"


class RecordingFetcher(BaseFetcher):
    """A fetcher whose behaviour is scripted per test."""

    name = "recording"

    def __init__(self, config, *, body=b"ok", status=200, error=None, available=True):
        super().__init__(config)
        self.body = body
        self.status = status
        self.error = error
        self._available = available
        self.calls = 0
        self.closed = False

    def is_available(self) -> bool:
        return self._available

    async def fetch(self, url, options):
        self.calls += 1
        if self.error is not None:
            error = self.error(self.calls) if callable(self.error) else self.error
            if error is not None:
                raise error
        return FetchResponse(
            url=url,
            status_code=self.status,
            headers={"content-type": "text/html"},
            content=self.body,
            content_type=ContentType.HTML,
            strategy_used=self.name,
        )

    async def aclose(self):
        self.closed = True


def build_chain(config: ScraperConfig, fetchers: dict[str, BaseFetcher]) -> FallbackChain:
    """Wire a chain to pre-built fetcher instances, bypassing the registry."""
    chain = FallbackChain.__new__(FallbackChain)
    chain.config = config
    chain._instances = dict(fetchers)
    return chain


@pytest.fixture
def options(config: ScraperConfig) -> FetchOptions:
    return FetchOptions().resolve(config)


def make_config(strategies: list[str], **kwargs) -> ScraperConfig:
    return ScraperConfig(
        strategies=strategies,
        respect_robots=False,
        min_delay=0.0,
        max_delay=0.0,
        max_retries=kwargs.pop("max_retries", 0),
        **kwargs,
    )


class TestEscalation:
    async def test_first_usable_response_wins(self, options):
        cfg = make_config(["a", "b"])
        a = RecordingFetcher(cfg, body=b"<html>from a</html>")
        b = RecordingFetcher(cfg, body=b"<html>from b</html>")
        chain = build_chain(cfg, {"a": a, "b": b})

        response = await chain.fetch(URL, options.model_copy(update={"strategies": ["a", "b"]}))

        assert response.text == "<html>from a</html>"
        assert b.calls == 0, "later strategies must not run once one succeeds"

    async def test_block_escalates_to_the_next_strategy(self, options, challenge_html):
        cfg = make_config(["a", "b"])
        a = RecordingFetcher(cfg, body=challenge_html.encode())
        b = RecordingFetcher(cfg, body=b"<html>real content</html>")
        chain = build_chain(cfg, {"a": a, "b": b})

        response = await chain.fetch(URL, options.model_copy(update={"strategies": ["a", "b"]}))

        assert a.calls == 1
        assert b.calls == 1
        assert response.text == "<html>real content</html>"

    async def test_block_does_not_burn_retries(self, options, challenge_html):
        """A challenge means escalate, not retry — the wall will not move."""
        cfg = make_config(["a", "b"], max_retries=3)
        a = RecordingFetcher(cfg, body=challenge_html.encode())
        b = RecordingFetcher(cfg, body=b"ok")
        chain = build_chain(cfg, {"a": a, "b": b})

        await chain.fetch(URL, options.model_copy(update={"strategies": ["a", "b"]}))

        assert a.calls == 1

    async def test_transient_error_is_retried_on_the_same_strategy(self, options):
        cfg = make_config(["a"], max_retries=2)
        # Fail twice, then succeed.
        a = RecordingFetcher(
            cfg, error=lambda n: FetchError("connection reset") if n <= 2 else None
        )
        chain = build_chain(cfg, {"a": a})

        response = await chain.fetch(URL, options.model_copy(update={"strategies": ["a"]}))

        assert a.calls == 3
        assert response.status_code == 200

    async def test_retries_are_bounded_then_escalate(self, options):
        cfg = make_config(["a", "b"], max_retries=1)
        a = RecordingFetcher(cfg, error=FetchError("always down"))
        b = RecordingFetcher(cfg, body=b"ok")
        chain = build_chain(cfg, {"a": a, "b": b})

        await chain.fetch(URL, options.model_copy(update={"strategies": ["a", "b"]}))

        assert a.calls == 2  # 1 attempt + 1 retry
        assert b.calls == 1

    async def test_proxy_error_is_not_retried(self, options):
        """Bad proxy credentials will not fix themselves on a second attempt."""
        cfg = make_config(["a", "b"], max_retries=3)
        a = RecordingFetcher(cfg, error=ProxyError("407 from proxy"))
        b = RecordingFetcher(cfg, body=b"ok")
        chain = build_chain(cfg, {"a": a, "b": b})

        await chain.fetch(URL, options.model_copy(update={"strategies": ["a", "b"]}))

        assert a.calls == 1


class TestAvailability:
    async def test_unavailable_strategy_is_skipped(self, options):
        cfg = make_config(["a", "b"])
        a = RecordingFetcher(cfg, available=False)
        b = RecordingFetcher(cfg, body=b"ok")
        chain = build_chain(cfg, {"a": a, "b": b})

        response = await chain.fetch(URL, options.model_copy(update={"strategies": ["a", "b"]}))

        assert a.calls == 0
        assert response.status_code == 200

    async def test_all_unavailable_reports_install_hints(self, options):
        cfg = make_config(["a"])
        a = RecordingFetcher(cfg, available=False)
        a.extra_name = "scrapeforge[browser]"
        chain = build_chain(cfg, {"a": a})

        with pytest.raises(AllStrategiesFailed) as exc_info:
            await chain.fetch(URL, options.model_copy(update={"strategies": ["a"]}))

        assert "scrapeforge[browser]" in str(exc_info.value)
        assert "a" in exc_info.value.attempts


class TestFailureReporting:
    async def test_all_blocked_raises_challenge_error(self, options, challenge_html):
        cfg = make_config(["a", "b"])
        a = RecordingFetcher(cfg, body=challenge_html.encode())
        b = RecordingFetcher(cfg, body=challenge_html.encode())
        chain = build_chain(cfg, {"a": a, "b": b})

        with pytest.raises(ChallengeError) as exc_info:
            await chain.fetch(URL, options.model_copy(update={"strategies": ["a", "b"]}))

        assert exc_info.value.signature == "just a moment"

    async def test_mixed_failures_raise_all_strategies_failed(self, options, challenge_html):
        cfg = make_config(["a", "b"])
        a = RecordingFetcher(cfg, body=challenge_html.encode())
        b = RecordingFetcher(cfg, error=FetchError("dns failure"))
        chain = build_chain(cfg, {"a": a, "b": b})

        with pytest.raises(AllStrategiesFailed) as exc_info:
            await chain.fetch(URL, options.model_copy(update={"strategies": ["a", "b"]}))

        attempts = exc_info.value.attempts
        assert "blocked" in attempts["a"]
        assert "dns failure" in attempts["b"]

    async def test_error_message_does_not_leak_proxy_credentials(self, options):
        cfg = make_config(["a"])
        a = RecordingFetcher(cfg, error=FetchError("upstream refused"))
        chain = build_chain(cfg, {"a": a})
        opts = options.model_copy(
            update={"strategies": ["a"], "proxy": "http://user:hunter2@proxy.example:8080"}
        )

        with pytest.raises(AllStrategiesFailed) as exc_info:
            await chain.fetch(URL, opts)

        assert "hunter2" not in str(exc_info.value)


class TestValidation:
    def test_unknown_strategy_rejected_at_construction(self):
        with pytest.raises(ConfigError, match="Unknown strategy"):
            FallbackChain(make_config(["nope"]))

    async def test_unknown_strategy_rejected_per_request(self, options):
        cfg = make_config(["a"])
        chain = build_chain(cfg, {"a": RecordingFetcher(cfg)})

        with pytest.raises(ConfigError, match="Unknown strategy"):
            await chain.fetch(URL, options.model_copy(update={"strategies": ["ghost"]}))

    async def test_aclose_closes_every_instantiated_fetcher(self, options):
        cfg = make_config(["a"])
        a = RecordingFetcher(cfg)
        chain = build_chain(cfg, {"a": a})

        await chain.aclose()

        assert a.closed is True


class TestRegistry:
    def test_register_and_use_a_custom_fetcher(self):
        class Custom(BaseFetcher):
            name = "custom-test-strategy"

            async def fetch(self, url, options):  # pragma: no cover - not called
                raise NotImplementedError

        try:
            register_fetcher(Custom)
            assert FETCHER_REGISTRY["custom-test-strategy"] is Custom
            chain = FallbackChain(make_config(["custom-test-strategy"]))
            assert isinstance(chain.get_fetcher("custom-test-strategy"), Custom)
        finally:
            FETCHER_REGISTRY.pop("custom-test-strategy", None)

    def test_registering_a_duplicate_name_is_rejected(self):
        class Duplicate(BaseFetcher):
            name = "http"

            async def fetch(self, url, options):  # pragma: no cover - not called
                raise NotImplementedError

        with pytest.raises(ConfigError, match="already registered"):
            register_fetcher(Duplicate)

    def test_fetcher_without_a_name_is_rejected(self):
        class Unnamed(BaseFetcher):
            async def fetch(self, url, options):  # pragma: no cover - not called
                raise NotImplementedError

        with pytest.raises(ConfigError, match="unique 'name'"):
            register_fetcher(Unnamed)
