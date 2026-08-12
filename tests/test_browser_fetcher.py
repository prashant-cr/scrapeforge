"""PlaywrightFetcher.

The profile/engine coherence checks run everywhere (no browser needed). The live
tests are gated behind an installed browser, per the testing conventions.
"""

from __future__ import annotations

import pytest

from scrapekit.config import FetchOptions, ScraperConfig
from scrapekit.fetchers.browser import PlaywrightFetcher
from scrapekit.fingerprint.stealth import BROWSER_TYPE_TO_FAMILY, build_init_script
from scrapekit.fingerprint.user_agents import USER_AGENTS
from scrapekit.models import ContentType


def browser_available() -> bool:
    """True only when Playwright *and* a launchable Chromium are present."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            browser.close()
        return True
    except Exception:
        return False


requires_browser = pytest.mark.skipif(
    not browser_available(), reason="Playwright browser not installed (run 'playwright install')"
)


def make_config(**kwargs) -> ScraperConfig:
    return ScraperConfig(
        strategies=["browser"], respect_robots=False, min_delay=0, max_delay=0, **kwargs
    )


class TestProfileEngineCoherence:
    """A Firefox UA on a Chromium engine is detectable from the JS surface alone."""

    @pytest.mark.parametrize("browser_type", ["chromium", "firefox", "webkit"])
    def test_profile_family_matches_the_launched_engine(self, browser_type):
        fetcher = PlaywrightFetcher(make_config(browser_type=browser_type))
        options = FetchOptions().resolve(fetcher.config)
        expected = BROWSER_TYPE_TO_FAMILY[browser_type]

        for _ in range(30):
            assert fetcher.select_profile(options).browser == expected

    def test_never_selects_a_mobile_profile_for_a_desktop_viewport(self):
        fetcher = PlaywrightFetcher(make_config(browser_type="chromium"))
        options = FetchOptions().resolve(fetcher.config)

        for _ in range(30):
            assert fetcher.select_profile(options).mobile is False

    def test_pinned_user_agent_still_wins(self):
        fetcher = PlaywrightFetcher(make_config(browser_type="chromium"))
        options = FetchOptions(headers={"User-Agent": "my-crawler/1.0"}).resolve(fetcher.config)

        assert fetcher.select_profile(options).user_agent == "my-crawler/1.0"

    def test_stable_profile_when_rotation_is_off(self):
        fetcher = PlaywrightFetcher(make_config(browser_type="chromium", rotate_user_agent=False))
        options = FetchOptions().resolve(fetcher.config)

        assert len({fetcher.select_profile(options).user_agent for _ in range(10)}) == 1


class TestInitScript:
    def test_chrome_only_apis_are_chromium_only(self):
        """window.chrome / deviceMemory on a Firefox profile is the very
        inconsistency these evasions exist to remove."""
        chrome = next(p for p in USER_AGENTS if p.browser == "chrome")
        firefox = next(p for p in USER_AGENTS if p.browser == "firefox")
        safari = next(p for p in USER_AGENTS if p.browser == "safari")

        chrome_script = build_init_script(chrome)
        assert "window.chrome" in chrome_script
        assert "deviceMemory" in chrome_script

        for script in (build_init_script(firefox), build_init_script(safari)):
            assert "window.chrome" not in script
            assert "deviceMemory" not in script

    def test_core_evasions_apply_to_every_profile(self):
        for profile in USER_AGENTS:
            script = build_init_script(profile)
            assert "webdriver" in script
            assert "getParameter" in script


@requires_browser
class TestLiveBrowser:
    async def test_get_renders_and_decodes(self):
        fetcher = PlaywrightFetcher(make_config())
        options = FetchOptions(wait_until="domcontentloaded").resolve(fetcher.config)
        try:
            response = await fetcher.fetch("https://example.com/", options)
        finally:
            await fetcher.aclose()

        assert response.status_code == 200
        assert response.strategy_used == "browser"
        assert "Example Domain" in response.text

    async def test_stealth_patches_are_applied_in_the_page(self):
        """The evasions must survive into the real page context, not just the string."""
        from playwright.async_api import async_playwright

        from scrapekit.fingerprint.stealth import launch_args

        fetcher = PlaywrightFetcher(make_config())
        profile = fetcher.select_profile(FetchOptions().resolve(fetcher.config))

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, args=launch_args())
            context = await browser.new_context(user_agent=profile.user_agent)
            await context.add_init_script(build_init_script(profile))
            page = await context.new_page()
            await page.goto("about:blank")
            probe = await page.evaluate(
                """() => ({
                    webdriver: navigator.webdriver === undefined,
                    webdriverGone: !('webdriver' in Object.getPrototypeOf(navigator)),
                    plugins: navigator.plugins.length,
                    platform: navigator.platform,
                    cores: navigator.hardwareConcurrency,
                    hasChrome: !!window.chrome,
                    webglVendor: (() => {
                        const c = document.createElement('canvas').getContext('webgl');
                        return c ? c.getParameter(37445) : null;
                    })(),
                })"""
            )
            await browser.close()

        assert probe["webdriver"] is True, "navigator.webdriver must be undefined"
        assert probe["webdriverGone"] is True, "webdriver must be gone from the prototype"
        assert probe["plugins"] > 0, "headless reports an empty plugin list"
        assert probe["cores"] == 8
        assert probe["hasChrome"] is True
        assert probe["webglVendor"] == "Intel Inc."

    async def test_get_on_a_json_endpoint_returns_parseable_json(self):
        """A browser wraps JSON in <pre>; the chain can escalate an API call here,
        so the raw body must survive rather than the DOM wrapper."""
        fetcher = PlaywrightFetcher(make_config())
        options = FetchOptions(wait_until="domcontentloaded").resolve(fetcher.config)
        try:
            response = await fetcher.fetch("https://postman-echo.com/get?probe=1", options)
        finally:
            await fetcher.aclose()

        assert response.status_code == 200
        assert response.content_type is ContentType.JSON
        assert not response.text.lstrip().startswith("<"), "DOM wrapper leaked into the body"
        assert response.json()["args"] == {"probe": "1"}

    async def test_get_on_html_still_returns_the_rendered_dom(self):
        fetcher = PlaywrightFetcher(make_config())
        options = FetchOptions(wait_until="domcontentloaded").resolve(fetcher.config)
        try:
            response = await fetcher.fetch("https://example.com/", options)
        finally:
            await fetcher.aclose()

        assert response.content_type is ContentType.HTML
        assert response.text.lstrip().lower().startswith("<!doctype html")

    async def test_post_travels_through_the_browser_stack(self):
        fetcher = PlaywrightFetcher(make_config())
        options = FetchOptions(method="POST", json={"a": 1}).resolve(fetcher.config)
        try:
            response = await fetcher.fetch("https://postman-echo.com/post", options)
        finally:
            await fetcher.aclose()

        assert response.status_code == 200
        assert response.json()["data"] == {"a": 1}
