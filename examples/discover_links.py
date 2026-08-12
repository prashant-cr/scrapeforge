"""Discover a site's links from its well-known files.

Reads ``robots.txt``, ``sitemap.xml`` (following nested indexes), ``llms.txt``,
and ``ai.txt``, and reports what each one advertised. This is discovery, not
crawling — the links are returned, never fetched — so it is a cheap way to see
what a site publishes before deciding what to scrape.

    python examples/discover_links.py [url]
"""

from __future__ import annotations

import asyncio
import sys

from scrapeforge import Scraper

DEFAULT_URL = "https://docs.anthropic.com"


async def main(url: str) -> None:
    async with Scraper(
        strategies=["http", "impersonate"],
        respect_robots=True,
        # Discovery can walk a lot of sitemaps; keep it bounded and polite.
        max_discovered_links=200,
        max_sitemap_documents=5,
        min_delay=0.3,
        max_delay=0.6,
        timeout=45,
    ) as scraper:
        manifest = await scraper.discover(url)

    print(f"\n{manifest.base_url}\n")

    print("what each well-known file gave us:")
    for report in manifest.reports:
        detail = f"  ({report.detail[:50]})" if report.detail else ""
        print(f"  {report.source.value:8} {report.status:10} links={report.link_count}{detail}")

    print(f"\ntotal unique links: {len(manifest.links)}")
    print(f"pages (excluding sitemaps): {len(manifest.page_urls)}")
    if manifest.truncated:
        # Never let a capped result look like a complete one.
        print("  NOTE: a limit stopped discovery early; raise max_discovered_links to see more")

    if manifest.robots:
        robots = manifest.robots
        print(
            f"\nrobots.txt: {len(robots.sitemaps)} sitemap(s), "
            f"{len(robots.disallow)} disallow rule(s), crawl-delay={robots.crawl_delay}"
        )
        for rule in robots.disallow[:5]:
            print(f"    disallow {rule}")

    if manifest.ai:
        ai = manifest.ai
        print(f"\nai.txt: opts out of everything = {ai.disallows_everything}")
        for key, values in list(ai.directives.items())[:5]:
            print(f"    {key}: {', '.join(values[:3])}")

    # llms.txt is curated by the site owner, so its links are usually the most
    # useful starting points — and they carry a section and description.
    curated = manifest.by_source("llms")
    if curated:
        print(f"\nllms.txt curated links ({len(curated)}), first few:")
        for link in curated[:8]:
            section = f"[{link.section}] " if link.section else ""
            title = link.title or link.url
            print(f"    {section}{title}")
            print(f"      {link.url}")

    pages = [link for link in manifest.links if not link.is_sitemap]
    if pages:
        print(f"\nfirst few pages ({len(pages)} total):")
        for link in pages[:8]:
            when = f"  (lastmod {link.lastmod})" if link.lastmod else ""
            print(f"    {link.url}{when}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL))
