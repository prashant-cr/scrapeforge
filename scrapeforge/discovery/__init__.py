"""Link discovery from a site's well-known files.

Four conventions, one result type::

    robots.txt    Sitemap: directives, plus the crawl rules the site published
    sitemap.xml   <urlset> pages and <sitemapindex> children, gzip included
    llms.txt      Markdown links, with their section and description
    ai.txt        AI-training permissions, robots-like in shape

Use it through :meth:`scrapeforge.Scraper.discover`, which supplies fetching,
rate limiting, and the fallback chain::

    manifest = await scraper.discover("https://example.com")
    manifest.page_urls          # every page URL advertised
    manifest.by_source("llms")  # just the curated llms.txt links
"""

from __future__ import annotations

from .discoverer import DEFAULT_SOURCES, WELL_KNOWN_PATHS, SiteDiscoverer
from .models import (
    AiTxtInfo,
    DiscoveredLink,
    LinkSource,
    RobotsInfo,
    SiteManifest,
    SourceReport,
)
from .parsers import parse_ai_txt, parse_llms_txt, parse_robots_txt, parse_sitemap

__all__ = [
    "DEFAULT_SOURCES",
    "WELL_KNOWN_PATHS",
    "AiTxtInfo",
    "DiscoveredLink",
    "LinkSource",
    "RobotsInfo",
    "SiteDiscoverer",
    "SiteManifest",
    "SourceReport",
    "parse_ai_txt",
    "parse_llms_txt",
    "parse_robots_txt",
    "parse_sitemap",
]
