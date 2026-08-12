"""Data model shared by every discovery source."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

__all__ = [
    "AiTxtInfo",
    "DiscoveredLink",
    "LinkSource",
    "RobotsInfo",
    "SiteManifest",
    "SourceReport",
]


class LinkSource(str, Enum):
    """Which well-known file a link came from."""

    ROBOTS = "robots"
    SITEMAP = "sitemap"
    LLMS = "llms"
    AI = "ai"


@dataclass(frozen=True)
class DiscoveredLink:
    """One URL found in a discovery document.

    Attributes:
        url: Absolute URL.
        source: Which file it came from.
        title: Link text (``llms.txt`` markdown links).
        description: Trailing description (``llms.txt``).
        section: The ``##`` heading it appeared under (``llms.txt``).
        lastmod: ``<lastmod>`` value, verbatim (sitemaps).
        changefreq: ``<changefreq>`` value (sitemaps).
        priority: ``<priority>`` value (sitemaps).
        is_sitemap: True when this URL points at another sitemap rather than a
            page — i.e. it came from a ``<sitemapindex>`` or a robots
            ``Sitemap:`` line.
        found_in: URL of the document this link was read from, which is what you
            need when debugging a deep sitemap-index tree.
    """

    url: str
    source: LinkSource
    title: str | None = None
    description: str | None = None
    section: str | None = None
    lastmod: str | None = None
    changefreq: str | None = None
    priority: float | None = None
    is_sitemap: bool = False
    found_in: str | None = None


@dataclass
class RobotsInfo:
    """Parsed ``robots.txt``.

    ``allow``/``disallow`` are the rules for the requested user agent, with the
    ``*`` group merged in as the fallback — the same precedence a crawler
    applies. Use :class:`~scrapeforge.utils.robots.RobotsCache` to *enforce*
    rules; this type is for reading what a site published.
    """

    url: str
    sitemaps: list[str] = field(default_factory=list)
    allow: list[str] = field(default_factory=list)
    disallow: list[str] = field(default_factory=list)
    crawl_delay: float | None = None
    user_agents: list[str] = field(default_factory=list)
    raw: str = ""


@dataclass
class AiTxtInfo:
    """Parsed ``ai.txt`` — an AI-training permissions file, robots-like in shape.

    This records what a site asked for. scrapeforge surfaces it so callers can
    honour it; it does not silently enforce it, because only the caller knows
    whether their use is training, inference, or neither.
    """

    url: str
    allow: list[str] = field(default_factory=list)
    disallow: list[str] = field(default_factory=list)
    user_agents: list[str] = field(default_factory=list)
    directives: dict[str, list[str]] = field(default_factory=dict)
    raw: str = ""

    @property
    def disallows_everything(self) -> bool:
        """True when the file opts out of everything for the agents it names."""
        return "/" in self.disallow


@dataclass
class SourceReport:
    """What happened when one source was tried.

    A missing file is not a failure — most sites publish none of these — so
    ``status`` distinguishes "absent" from "broken" rather than collapsing both
    into an empty result.
    """

    source: LinkSource
    url: str
    status: str  # "ok" | "not_found" | "error" | "skipped"
    link_count: int = 0
    detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"


@dataclass
class SiteManifest:
    """Everything discovery found for one site.

    Attributes:
        base_url: Origin the discovery ran against.
        links: Every link found, in discovery order, de-duplicated by URL.
        reports: One entry per source attempted, including the ones that were
            absent — silence about a missing file would look like a site with no
            sitemap, which is a different thing.
        robots: Parsed ``robots.txt``, when present.
        ai: Parsed ``ai.txt``, when present.
        truncated: True when a limit stopped discovery early, so a short result
            is never mistaken for a complete one.
    """

    base_url: str
    links: list[DiscoveredLink] = field(default_factory=list)
    reports: list[SourceReport] = field(default_factory=list)
    robots: RobotsInfo | None = None
    ai: AiTxtInfo | None = None
    truncated: bool = False

    @property
    def urls(self) -> list[str]:
        """Just the URLs, order preserved."""
        return [link.url for link in self.links]

    @property
    def page_urls(self) -> list[str]:
        """Links to actual pages, excluding pointers to other sitemaps."""
        return [link.url for link in self.links if not link.is_sitemap]

    def by_source(self, source: LinkSource | str) -> list[DiscoveredLink]:
        """Links that came from one particular file."""
        wanted = LinkSource(source)
        return [link for link in self.links if link.source is wanted]

    def report_for(self, source: LinkSource | str) -> SourceReport | None:
        wanted = LinkSource(source)
        return next((r for r in self.reports if r.source is wanted), None)

    def __len__(self) -> int:
        return len(self.links)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        counts = ", ".join(f"{r.source.value}={r.link_count}" for r in self.reports if r.link_count)
        return (
            f"<SiteManifest {self.base_url!r} links={len(self.links)}"
            f"{' ' + counts if counts else ''}"
            f"{' truncated' if self.truncated else ''}>"
        )
