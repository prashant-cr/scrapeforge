"""Render a JavaScript-heavy page with the stealth browser fetcher.

Requires the browser extra:

    pip install "scrapekit[browser]"
    playwright install chromium
    python examples/browser_stealth.py
"""

from __future__ import annotations

import asyncio

from scrapekit import ChallengeError, ConfigError, FetchError, Scraper

URL = "https://example.com/"


async def main() -> None:
    async with Scraper(
        # Force the browser rung. Normally you would leave the full chain in
        # place and let it escalate here only when the cheaper rungs are blocked.
        strategies=["browser"],
        browser_type="chromium",
        headless=True,
        rotate_user_agent=True,
        respect_robots=True,
        timeout=45,
    ) as scraper:
        if not scraper.chain.get_fetcher("browser").is_available():
            print("Playwright is not installed. Run: pip install 'scrapekit[browser]'")
            return

        try:
            response = await scraper.fetch(
                URL,
                # Waiting strategy, per request:
                #   wait_until          - "load" | "domcontentloaded" | "networkidle" | "commit"
                #   wait_for_selector   - block until this element exists
                #   wait_time           - extra seconds for lazily-loaded content
                wait_until="networkidle",
                wait_for_selector="body",
                wait_time=0.5,
            )
        except ChallengeError as exc:
            # The browser is the last rung. If it is also challenged, scrapekit
            # stops and hands the decision back to you rather than trying to
            # defeat the challenge.
            print(f"Still challenged at the browser rung (matched {exc.signature!r})")
            return
        except ConfigError as exc:
            print(f"Browser unavailable: {exc}")
            return
        except FetchError as exc:
            print(f"Fetch failed: {exc}")
            return

        print(f"status    {response.status_code}")
        print(f"strategy  {response.strategy_used}")
        print(f"rendered  {len(response.content)} bytes of post-JS DOM")


if __name__ == "__main__":
    asyncio.run(main())
