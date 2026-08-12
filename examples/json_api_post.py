"""POST to a JSON API with custom headers, cookies, and a proxy.

Shows the per-request override path: client defaults set once, individual calls
overriding what they need. Headers and cookies merge; scalars replace.

    python examples/json_api_post.py
"""

from __future__ import annotations

import asyncio

from scrapeforge import ContentType, FetchError, Scraper


async def main() -> None:
    scraper = Scraper(
        strategies=["http", "impersonate"],
        # Client-wide defaults, applied to every request.
        headers={"Accept-Language": "en-US,en;q=0.9", "X-Client": "scrapeforge-example"},
        cookies={"consent": "granted"},
        timeout=30,
        max_retries=2,
        # proxy="http://user:pass@host:port",   # credentials are never logged
        respect_robots=True,
    )

    async with scraper:
        try:
            response = await scraper.fetch(
                # Any echo endpoint works; postman-echo is the more reliable of the two.
                "https://postman-echo.com/post",  # or https://httpbin.org/post
                method="POST",
                json={"q": "shoes", "size": 42},
                # Per-request overrides. X-Client survives from the config,
                # X-Scrapeforge-Request is added, and timeout replaces the default.
                headers={"X-Scrapeforge-Request": "abc-123"},
                cookies={"session": "s3cret"},
                timeout=15,
                # Tells the chain to escalate if an HTML block page comes back
                # where JSON was expected.
                expected_content_type=ContentType.JSON,
            )
        except FetchError as exc:
            print(f"Request failed: {exc}")
            return

        print(f"status    {response.status_code}")
        print(f"strategy  {response.strategy_used}")
        print(f"type      {response.content_type.value}")

        if response.content_type is ContentType.JSON:
            payload = response.json()
            # Echo services differ on header casing, and HTTP headers are
            # case-insensitive anyway — normalize before looking anything up.
            seen = {k.lower(): v for k, v in (payload.get("headers") or {}).items()}
            print("\nheaders the server saw:")
            for key in ("x-client", "x-scrapeforge-request", "accept-language", "user-agent"):
                print(f"  {key}: {seen.get(key)}")
            print(f"\nbody echoed back: {payload.get('json') or payload.get('data')}")


if __name__ == "__main__":
    asyncio.run(main())
