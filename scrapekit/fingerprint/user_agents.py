"""Bundled user-agent pool.

The pool is offline and bundled on purpose: tests stay deterministic, and a
scrape never depends on a third-party UA feed being reachable. ``fake-useragent``
can be layered on top by the caller if they want a fresher pool, but the bundled
set is always the fallback.

Each entry carries the metadata needed to build a *coherent* header set — brand
list, platform, mobile flag, and a matching ``curl_cffi`` impersonation target.
Incoherent combinations (a Chrome UA with Firefox-only headers) are a strong
bot signal, so the profile, not the raw string, is the unit we pass around.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

__all__ = ["USER_AGENTS", "UserAgentProfile", "profile_for_user_agent", "random_profile"]


@dataclass(frozen=True)
class UserAgentProfile:
    """A user agent plus everything needed to build headers consistent with it.

    Attributes:
        user_agent: The ``User-Agent`` header value.
        browser: ``"chrome"``, ``"firefox"``, or ``"safari"``.
        major_version: Major browser version, as a string.
        platform: Value for ``Sec-CH-UA-Platform`` (unquoted).
        mobile: Whether this is a mobile profile.
        impersonate: Matching ``curl_cffi`` impersonation target.
        viewport: ``(width, height)`` used by the browser fetcher.
        accept_language: Default ``Accept-Language`` for this profile.
    """

    user_agent: str
    browser: str
    major_version: str
    platform: str
    mobile: bool = False
    impersonate: str = "chrome124"
    viewport: tuple[int, int] = (1920, 1080)
    accept_language: str = "en-US,en;q=0.9"
    brands: tuple[tuple[str, str], ...] = field(default=())

    @property
    def sec_ch_ua(self) -> str | None:
        """The ``Sec-CH-UA`` header value, or ``None`` for non-Chromium browsers."""
        if not self.brands:
            return None
        return ", ".join(f'"{name}";v="{version}"' for name, version in self.brands)

    @property
    def is_chromium(self) -> bool:
        return self.browser == "chrome"


def _chrome(
    version: str,
    ua: str,
    platform: str,
    *,
    mobile: bool = False,
    impersonate: str = "chrome124",
    viewport: tuple[int, int] = (1920, 1080),
) -> UserAgentProfile:
    """Build a Chromium profile with a plausible brand list for ``version``."""
    return UserAgentProfile(
        user_agent=ua,
        browser="chrome",
        major_version=version,
        platform=platform,
        mobile=mobile,
        impersonate=impersonate,
        viewport=viewport,
        brands=(
            ("Chromium", version),
            ("Google Chrome", version),
            ("Not_A Brand", "24"),
        ),
    )


#: Bundled desktop + mobile pool. Kept small and current rather than exhaustive;
#: a huge pool of stale strings is worse than a handful of realistic ones.
USER_AGENTS: tuple[UserAgentProfile, ...] = (
    _chrome(
        "131",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36",
        "Windows",
        impersonate="chrome131",
    ),
    _chrome(
        "131",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36",
        "macOS",
        impersonate="chrome131",
        viewport=(1728, 1117),
    ),
    _chrome(
        "124",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36",
        "Linux",
        impersonate="chrome124",
        viewport=(1920, 1080),
    ),
    _chrome(
        "131",
        "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Mobile Safari/537.36",
        "Android",
        mobile=True,
        impersonate="chrome131_android",
        viewport=(412, 915),
    ),
    UserAgentProfile(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
        browser="firefox",
        major_version="133",
        platform="Windows",
        impersonate="firefox133",
        viewport=(1920, 1080),
    ),
    UserAgentProfile(
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:133.0) Gecko/20100101 Firefox/133.0",
        browser="firefox",
        major_version="133",
        platform="macOS",
        impersonate="firefox133",
        viewport=(1728, 1117),
    ),
    UserAgentProfile(
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/18.1 Safari/605.1.15",
        browser="safari",
        major_version="18",
        platform="macOS",
        impersonate="safari18_0",
        viewport=(1728, 1117),
    ),
    UserAgentProfile(
        user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/18.1 Mobile/15E148 Safari/604.1",
        browser="safari",
        major_version="18",
        platform="iOS",
        mobile=True,
        impersonate="safari18_0_ios",
        viewport=(393, 852),
    ),
)

_BY_UA: dict[str, UserAgentProfile] = {profile.user_agent: profile for profile in USER_AGENTS}

#: Used when the caller pins a UA we have no metadata for.
DEFAULT_PROFILE: UserAgentProfile = USER_AGENTS[0]


def random_profile(
    *, desktop_only: bool = True, rng: random.Random | None = None
) -> UserAgentProfile:
    """Pick a profile from the bundled pool.

    Args:
        desktop_only: Exclude mobile profiles. Mobile UAs paired with a desktop
            viewport are an obvious inconsistency, so this defaults to ``True``.
        rng: Random source, for deterministic tests.

    Returns:
        A :class:`UserAgentProfile`.
    """
    candidates = [p for p in USER_AGENTS if not (desktop_only and p.mobile)]
    return (rng or random).choice(candidates)


def profile_for_user_agent(user_agent: str) -> UserAgentProfile:
    """Return the bundled profile for ``user_agent``, inferring one if unknown.

    A caller-supplied UA always wins, but we still need coherent companion
    headers — so an unknown string is classified by family and given a
    plausible profile rather than being dropped.
    """
    known = _BY_UA.get(user_agent)
    if known is not None:
        return known

    lowered = user_agent.lower()
    mobile = "mobile" in lowered or "android" in lowered or "iphone" in lowered

    if "firefox/" in lowered:
        browser, impersonate = "firefox", "firefox133"
    elif "chrome/" in lowered or "chromium/" in lowered:
        browser, impersonate = "chrome", "chrome131"
    elif "safari/" in lowered:
        browser, impersonate = "safari", "safari18_0"
    else:
        browser, impersonate = "chrome", "chrome131"

    if "windows" in lowered:
        platform = "Windows"
    elif "mac os x" in lowered or "macintosh" in lowered:
        platform = "macOS"
    elif "android" in lowered:
        platform = "Android"
    elif "iphone" in lowered or "ipad" in lowered:
        platform = "iOS"
    elif "linux" in lowered:
        platform = "Linux"
    else:
        platform = "Windows"

    brands: tuple[tuple[str, str], ...] = ()
    version = "0"
    if browser == "chrome":
        marker = "chrome/"
        start = lowered.find(marker)
        if start != -1:
            version = lowered[start + len(marker) :].split(".", 1)[0] or "131"
            brands = (("Chromium", version), ("Google Chrome", version), ("Not_A Brand", "24"))

    return UserAgentProfile(
        user_agent=user_agent,
        browser=browser,
        major_version=version,
        platform=platform,
        mobile=mobile,
        impersonate=impersonate,
        viewport=(393, 852) if mobile else (1920, 1080),
        brands=brands,
    )
