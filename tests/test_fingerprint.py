"""User-agent rotation and header coherence."""

from __future__ import annotations

import random

import pytest

from scrapekit.fingerprint import build_headers, profile_for_user_agent, random_profile
from scrapekit.fingerprint.headers import SUPPORTED_ENCODINGS
from scrapekit.fingerprint.stealth import build_init_script, launch_args
from scrapekit.fingerprint.user_agents import USER_AGENTS
from scrapekit.models import ContentType


class TestUserAgentPool:
    def test_pool_is_non_empty_and_offline(self):
        assert len(USER_AGENTS) >= 4

    def test_desktop_only_excludes_mobile(self):
        rng = random.Random(1234)
        for _ in range(50):
            assert random_profile(desktop_only=True, rng=rng).mobile is False

    def test_rotation_produces_more_than_one_profile(self):
        rng = random.Random(7)
        seen = {random_profile(rng=rng).user_agent for _ in range(50)}
        assert len(seen) > 1

    def test_every_profile_is_self_consistent(self):
        for profile in USER_AGENTS:
            ua = profile.user_agent.lower()
            if profile.browser == "firefox":
                assert "firefox/" in ua
                assert profile.sec_ch_ua is None, "Firefox does not send Sec-CH-UA"
            elif profile.browser == "chrome":
                assert "chrome/" in ua
                assert profile.sec_ch_ua is not None
                assert profile.major_version in profile.sec_ch_ua
            else:
                assert "safari/" in ua
                assert profile.sec_ch_ua is None, "Safari does not send Sec-CH-UA"
            assert profile.mobile == ("mobile" in ua or "android" in ua or "iphone" in ua)

    def test_known_ua_round_trips_to_its_profile(self):
        original = USER_AGENTS[0]
        assert profile_for_user_agent(original.user_agent) is original

    def test_unknown_ua_is_classified_not_dropped(self):
        profile = profile_for_user_agent(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/999.0.0.0 Safari/537.36"
        )
        assert profile.browser == "chrome"
        assert profile.platform == "Windows"
        assert profile.major_version == "999"
        assert profile.sec_ch_ua is not None and '"999"' in profile.sec_ch_ua


class TestBuildHeaders:
    def test_chrome_headers_are_coherent(self):
        profile = next(p for p in USER_AGENTS if p.browser == "chrome" and not p.mobile)
        headers = build_headers(profile)
        assert headers["user-agent"] == profile.user_agent
        assert headers["sec-ch-ua"] == profile.sec_ch_ua
        assert headers["sec-ch-ua-mobile"] == "?0"
        assert headers["sec-ch-ua-platform"] == f'"{profile.platform}"'
        assert headers["sec-fetch-dest"] == "document"

    def test_firefox_gets_no_chromium_only_headers(self):
        profile = next(p for p in USER_AGENTS if p.browser == "firefox")
        headers = build_headers(profile)
        assert "sec-ch-ua" not in headers
        assert "priority" not in headers
        assert "firefox" in headers["user-agent"].lower()

    def test_mobile_profile_sets_mobile_hint(self):
        profile = next(p for p in USER_AGENTS if p.mobile and p.browser == "chrome")
        assert build_headers(profile)["sec-ch-ua-mobile"] == "?1"

    def test_json_expectation_changes_accept(self):
        profile = USER_AGENTS[0]
        headers = build_headers(profile, expected_content_type=ContentType.JSON)
        assert headers["accept"] == "application/json, text/plain, */*"

    def test_xhr_request_uses_cors_fetch_metadata(self):
        headers = build_headers(USER_AGENTS[0], is_navigation=False)
        assert headers["sec-fetch-mode"] == "cors"
        assert headers["sec-fetch-dest"] == "empty"
        assert "upgrade-insecure-requests" not in headers

    def test_referer_flips_fetch_site(self):
        headers = build_headers(USER_AGENTS[0], referer="https://example.com/")
        assert headers["referer"] == "https://example.com/"
        assert headers["sec-fetch-site"] == "same-origin"

    def test_user_headers_override_generated_ones(self):
        profile = USER_AGENTS[0]
        headers = build_headers(profile, extra={"User-Agent": "custom-agent/1.0"})
        # Overriding is case-insensitive and must not leave a duplicate behind.
        assert headers["user-agent"] == "custom-agent/1.0"
        assert sum(1 for k in headers if k.lower() == "user-agent") == 1

    def test_extra_headers_are_added(self):
        headers = build_headers(USER_AGENTS[0], extra={"X-Trace-Id": "abc"})
        assert headers["X-Trace-Id"] == "abc"

    @pytest.mark.parametrize("profile", USER_AGENTS, ids=lambda p: f"{p.browser}-{p.platform}")
    def test_only_decodable_encodings_are_advertised(self, profile):
        """Advertising an encoding we cannot decode returns raw compressed bytes."""
        advertised = {e.strip() for e in build_headers(profile)["accept-encoding"].split(",")}
        assert advertised <= set(SUPPORTED_ENCODINGS)
        assert "gzip" in advertised

    def test_brotli_and_zstd_decoders_are_installed(self):
        # These are core dependencies precisely so the browser-like
        # Accept-Encoding list above is honest.
        assert "br" in SUPPORTED_ENCODINGS
        assert "zstd" in SUPPORTED_ENCODINGS

    def test_zstd_is_chrome_only(self):
        """Firefox and Safari do not send zstd; claiming it is an incoherence."""
        chrome = next(p for p in USER_AGENTS if p.browser == "chrome")
        firefox = next(p for p in USER_AGENTS if p.browser == "firefox")
        assert "zstd" in build_headers(chrome)["accept-encoding"]
        assert "zstd" not in build_headers(firefox)["accept-encoding"]


class TestStealth:
    def test_launch_args_disable_automation_flag(self):
        args = launch_args()
        assert "--disable-blink-features=AutomationControlled" in args

    def test_launch_args_accepts_extras(self):
        assert "--foo" in launch_args(extra=["--foo"])

    def test_init_script_matches_the_profile(self):
        mac = next(p for p in USER_AGENTS if p.platform == "macOS")
        script = build_init_script(mac)
        assert "'MacIntel'" in script
        assert "webdriver" in script
        assert "getParameter" in script  # WebGL vendor/renderer normalization

    def test_mobile_profile_reports_touch_points(self):
        mobile = next(p for p in USER_AGENTS if p.mobile)
        assert "maxTouchPoints', 5" in build_init_script(mobile)


@pytest.mark.parametrize("profile", USER_AGENTS, ids=lambda p: f"{p.browser}-{p.platform}")
def test_headers_never_mix_browser_families(profile):
    """A Chrome UA with Firefox-only headers (or vice versa) is a red flag."""
    headers = build_headers(profile)
    ua = headers["user-agent"].lower()
    if "sec-ch-ua" in headers:
        assert "chrome/" in ua or "chromium/" in ua
