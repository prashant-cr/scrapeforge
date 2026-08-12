"""Shared test helpers."""

from __future__ import annotations

from functools import lru_cache

import pytest

__all__ = ["browser_available", "requires_browser"]


@lru_cache(maxsize=1)
def browser_available() -> bool:
    """True only when Playwright *and* a launchable Chromium are present.

    Cached because it actually launches a browser: importing this from several
    test modules should cost one launch for the whole session, not one each.
    """
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
    not browser_available(),
    reason="Playwright browser not installed (run 'playwright install chromium')",
)
