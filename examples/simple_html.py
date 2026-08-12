"""Fetch a page through the fallback chain and inspect the raw response.

No LLM involved — this shows the fetching layer on its own: which strategy won,
what content type came back, and how escalation is reported when it fails.

    python examples/simple_html.py
"""

from __future__ import annotations

import asyncio
import logging

from scrapekit import ChallengeError, FetchError, Scraper

# Turn on debug logging to watch the chain escalate strategy by strategy.
logging.basicConfig(level=logging.DEBUG, format="%(levelname)-5s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)

URL = "https://example.com/"


async def main() -> None:
    async with Scraper(
        # Escalation order. Drop "browser" if Playwright is not installed —
        # a missing dependency is skipped, not fatal.
        strategies=["http", "impersonate", "browser"],
        timeout=20,
        # Polite by default: robots.txt is honored and requests to one host are
        # spaced out. Both are configurable.
        respect_robots=True,
        min_delay=0.5,
        max_delay=1.5,
    ) as scraper:
        try:
            response = await scraper.fetch(URL)
        except ChallengeError as exc:
            # scrapekit detects challenges but never solves them. Decide here:
            # a different proxy, your own solver, or back off.
            print(f"Blocked by bot management (matched {exc.signature!r})")
            return
        except FetchError as exc:
            print(f"Fetch failed: {exc}")
            return

        print(f"status        {response.status_code}")
        print(f"final url     {response.url}")
        print(f"content type  {response.content_type.value}")
        print(f"strategy      {response.strategy_used}")
        print(f"elapsed       {response.elapsed:.2f}s")
        print(f"bytes         {len(response.content)}")
        print("---")
        print(response.text[:400])


if __name__ == "__main__":
    asyncio.run(main())
