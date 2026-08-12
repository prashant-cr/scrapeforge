"""scrapeforge — schema-driven scraping with layered evasion and LLM extraction.

Quick start::

    from pydantic import BaseModel
    from scrapeforge import Scraper

    class Product(BaseModel):
        name: str
        price: float

    scraper = Scraper(llm_provider="anthropic", llm_model="claude-opus-5")
    product = await scraper.scrape("https://shop.example/p/1", schema=Product)

You are responsible for complying with each site's Terms of Service, applicable
law, and data-protection rules. See the README's responsible-use section.
"""

from __future__ import annotations

from .client import Scraper
from .config import FetchOptions, ScraperConfig
from .discovery import (
    AiTxtInfo,
    DiscoveredLink,
    LinkSource,
    RobotsInfo,
    SiteDiscoverer,
    SiteManifest,
    SourceReport,
)
from .exceptions import (
    AllStrategiesFailed,
    ChallengeError,
    ConfigError,
    FetchError,
    ParseError,
    ProxyError,
    ScrapeforgeError,
)
from .fetchers.base import BaseFetcher
from .fetchers.chain import FallbackChain, register_fetcher
from .models import ContentType, FetchResponse
from .parsers.base import BaseParser

__version__ = "0.2.0"

__all__ = [
    "AiTxtInfo",
    "AllStrategiesFailed",
    "BaseFetcher",
    "BaseParser",
    "ChallengeError",
    "ConfigError",
    "ContentType",
    "DiscoveredLink",
    "FallbackChain",
    "FetchError",
    "FetchOptions",
    "FetchResponse",
    "LinkSource",
    "ParseError",
    "ProxyError",
    "RobotsInfo",
    "ScrapeforgeError",
    "Scraper",
    "ScraperConfig",
    "SiteDiscoverer",
    "SiteManifest",
    "SourceReport",
    "__version__",
    "register_fetcher",
]
