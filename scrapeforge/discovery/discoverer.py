"""Fetching, recursion, and limits for site discovery.

The parsers in :mod:`scrapeforge.discovery.parsers` are pure; this is the part
that talks to the network. Its job is mostly restraint: sitemap indexes nest,
individual sitemaps run to 50MB and 50,000 URLs, and a cycle between two indexes
would otherwise loop forever. Every bound here is explicit and reported, so a
short result is never silently mistaken for a complete one.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable, Sequence
from urllib.parse import urljoin, urlsplit, urlunsplit

from ..exceptions import ConfigError, FetchError, ParseError, ScrapeforgeError
from ..models import FetchResponse
from .models import (
    AiTxtInfo,
    DiscoveredLink,
    LinkSource,
    RobotsInfo,
    SiteManifest,
    SourceReport,
)
from .parsers import parse_ai_txt, parse_llms_txt, parse_robots_txt, parse_sitemap

__all__ = ["DEFAULT_SOURCES", "WELL_KNOWN_PATHS", "SiteDiscoverer"]

logger = logging.getLogger(__name__)

Fetcher = Callable[..., Awaitable[FetchResponse]]

#: Conventional locations for each file.
WELL_KNOWN_PATHS: dict[LinkSource, tuple[str, ...]] = {
    LinkSource.ROBOTS: ("/robots.txt",),
    # Only tried when robots.txt advertises no sitemap of its own.
    LinkSource.SITEMAP: ("/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml"),
    LinkSource.LLMS: ("/llms.txt",),
    LinkSource.AI: ("/ai.txt",),
}

DEFAULT_SOURCES: tuple[LinkSource, ...] = (
    LinkSource.ROBOTS,
    LinkSource.SITEMAP,
    LinkSource.LLMS,
    LinkSource.AI,
)


class SiteDiscoverer:
    """Reads a site's well-known files and returns the links they advertise.

    Args:
        fetch: Async callable ``(url, **options) -> FetchResponse``. In practice
            this is :meth:`scrapeforge.Scraper.fetch`, so discovery inherits the
            fallback chain, proxy, and rate limiting.
        user_agent: Product token whose rules to read out of ``robots.txt`` and
            ``ai.txt``.
        max_sitemap_documents: Ceiling on sitemap files fetched, index documents
            included.
        max_depth: How far to follow nested sitemap indexes.
        max_links: Ceiling on links returned across all sources.
        max_document_bytes: Documents larger than this are skipped rather than
            parsed.
    """

    def __init__(
        self,
        fetch: Fetcher,
        *,
        user_agent: str = "*",
        max_sitemap_documents: int = 20,
        max_depth: int = 3,
        max_links: int = 10_000,
        max_document_bytes: int = 10 * 1024 * 1024,
    ) -> None:
        if max_sitemap_documents < 1 or max_depth < 0 or max_links < 1:
            raise ConfigError("discovery limits must be positive")
        self._fetch = fetch
        self.user_agent = user_agent
        self.max_sitemap_documents = max_sitemap_documents
        self.max_depth = max_depth
        self.max_links = max_links
        self.max_document_bytes = max_document_bytes

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def origin_of(url: str) -> str:
        """Return ``scheme://host[:port]`` for ``url``."""
        parts = urlsplit(url if "://" in url else f"https://{url}")
        if not parts.netloc:
            raise ConfigError(f"cannot determine an origin from {url!r}")
        return urlunsplit((parts.scheme or "https", parts.netloc, "", "", ""))

    async def _get(self, url: str) -> FetchResponse | None:
        """Fetch a well-known file. ``None`` means "absent", which is normal.

        ``respect_robots`` is off for these specific paths on purpose: they exist
        to be read by crawlers, a ``robots.txt`` that forbids reading itself is
        incoherent, and a sitemap is advertised precisely so it will be fetched.
        Links *discovered* here are returned, never followed, so the caller's
        robots policy still governs anything they go on to scrape.
        """
        response = await self._fetch(url, respect_robots=False)
        if response.status_code == 404 or response.status_code == 410:
            return None
        if not response.ok:
            raise FetchError(
                f"unexpected status for {url}", url=url, status_code=response.status_code
            )
        if len(response.content) > self.max_document_bytes:
            raise ParseError(
                f"document is {len(response.content)} bytes, over the "
                f"{self.max_document_bytes}-byte limit; skipped"
            )
        return response

    # -- per-source -------------------------------------------------------

    async def fetch_robots(self, base: str) -> tuple[RobotsInfo | None, SourceReport]:
        """Read and parse ``robots.txt``, including its ``Sitemap:`` directives."""
        url = urljoin(base + "/", WELL_KNOWN_PATHS[LinkSource.ROBOTS][0].lstrip("/"))
        try:
            response = await self._get(url)
        except ScrapeforgeError as exc:
            return None, SourceReport(LinkSource.ROBOTS, url, "error", detail=str(exc))
        if response is None:
            return None, SourceReport(LinkSource.ROBOTS, url, "not_found")

        info = parse_robots_txt(response.text, url=url, user_agent=self.user_agent)
        return info, SourceReport(LinkSource.ROBOTS, url, "ok", link_count=len(info.sitemaps))

    async def fetch_llms(
        self, base: str, *, path: str = "/llms.txt"
    ) -> tuple[list[DiscoveredLink], SourceReport]:
        """Read and parse ``llms.txt``."""
        url = urljoin(base + "/", path.lstrip("/"))
        try:
            response = await self._get(url)
        except ScrapeforgeError as exc:
            return [], SourceReport(LinkSource.LLMS, url, "error", detail=str(exc))
        if response is None:
            return [], SourceReport(LinkSource.LLMS, url, "not_found")

        links = parse_llms_txt(response.text, url=url)
        return links, SourceReport(LinkSource.LLMS, url, "ok", link_count=len(links))

    async def fetch_ai(self, base: str) -> tuple[AiTxtInfo | None, SourceReport]:
        """Read and parse ``ai.txt``."""
        url = urljoin(base + "/", WELL_KNOWN_PATHS[LinkSource.AI][0].lstrip("/"))
        try:
            response = await self._get(url)
        except ScrapeforgeError as exc:
            return None, SourceReport(LinkSource.AI, url, "error", detail=str(exc))
        if response is None:
            return None, SourceReport(LinkSource.AI, url, "not_found")

        info = parse_ai_txt(response.text, url=url, user_agent=self.user_agent)
        return info, SourceReport(LinkSource.AI, url, "ok")

    async def fetch_sitemaps(
        self,
        seeds: Sequence[str],
        *,
        follow_index: bool = True,
        budget: int | None = None,
    ) -> tuple[list[DiscoveredLink], list[SourceReport], bool]:
        """Walk one or more sitemaps, following indexes breadth-first.

        Breadth-first on purpose: an index usually lists its most useful child
        first, so if a limit cuts the walk short the caller keeps the top of the
        tree rather than one deep branch of it.

        Args:
            seeds: Sitemap URLs to start from.
            follow_index: Whether to descend into ``sitemapindex`` documents.
            budget: Link ceiling for this walk; defaults to ``max_links``.

        Returns:
            ``(links, reports, truncated)``.
        """
        limit = self.max_links if budget is None else budget
        links: list[DiscoveredLink] = []
        reports: list[SourceReport] = []
        seen_urls: set[str] = set()
        visited: set[str] = set()
        truncated = False

        queue: list[tuple[str, int]] = [(u, 0) for u in dict.fromkeys(seeds)]
        fetched = 0

        while queue:
            url, depth = queue.pop(0)
            if url in visited:
                continue
            visited.add(url)

            if fetched >= self.max_sitemap_documents:
                truncated = True
                reports.append(
                    SourceReport(
                        LinkSource.SITEMAP,
                        url,
                        "skipped",
                        detail=f"document limit of {self.max_sitemap_documents} reached",
                    )
                )
                break

            try:
                response = await self._get(url)
            except ScrapeforgeError as exc:
                reports.append(SourceReport(LinkSource.SITEMAP, url, "error", detail=str(exc)))
                continue
            if response is None:
                reports.append(SourceReport(LinkSource.SITEMAP, url, "not_found"))
                continue

            fetched += 1
            try:
                found = parse_sitemap(response.content, url=response.url)
            except ParseError as exc:
                reports.append(SourceReport(LinkSource.SITEMAP, url, "error", detail=str(exc)))
                continue

            added = 0
            for link in found:
                if link.is_sitemap:
                    if follow_index and depth < self.max_depth and link.url not in visited:
                        queue.append((link.url, depth + 1))
                    elif not follow_index:
                        pass  # still recorded below, just not followed
                    elif depth >= self.max_depth:
                        truncated = True
                if link.url in seen_urls:
                    continue
                if len(links) >= limit:
                    truncated = True
                    break
                seen_urls.add(link.url)
                links.append(link)
                added += 1

            reports.append(SourceReport(LinkSource.SITEMAP, url, "ok", link_count=added))
            if len(links) >= limit:
                truncated = True
                break

        return links, reports, truncated

    # -- orchestration ----------------------------------------------------

    async def discover(
        self,
        url: str,
        *,
        sources: Iterable[LinkSource | str] = DEFAULT_SOURCES,
        follow_sitemap_index: bool = True,
        include_llms_full: bool = False,
    ) -> SiteManifest:
        """Read every requested well-known file for ``url``'s origin.

        Sources are independent: a missing ``llms.txt`` does not stop the sitemap
        walk, and every attempt — including the absent ones — is recorded in
        :attr:`~scrapeforge.discovery.models.SiteManifest.reports`.

        Args:
            url: Any URL on the target site; only its origin is used.
            sources: Which files to consult.
            follow_sitemap_index: Descend into nested sitemap indexes.
            include_llms_full: Also read ``/llms-full.txt``, the expanded variant.

        Returns:
            A :class:`~scrapeforge.discovery.models.SiteManifest`.
        """
        wanted = [LinkSource(s) for s in sources]
        base = self.origin_of(url)
        manifest = SiteManifest(base_url=base)

        robots_info: RobotsInfo | None = None
        if LinkSource.ROBOTS in wanted:
            robots_info, report = await self.fetch_robots(base)
            manifest.robots = robots_info
            manifest.reports.append(report)
            if robots_info:
                for sitemap_url in robots_info.sitemaps:
                    manifest.links.append(
                        DiscoveredLink(
                            url=sitemap_url,
                            source=LinkSource.ROBOTS,
                            is_sitemap=True,
                            found_in=robots_info.url,
                        )
                    )

        if LinkSource.SITEMAP in wanted:
            # Prefer what robots.txt advertises; only guess conventional paths
            # when it names none, to avoid pointless 404s on every scrape.
            seeds = (
                list(robots_info.sitemaps)
                if robots_info and robots_info.sitemaps
                else [
                    urljoin(base + "/", p.lstrip("/")) for p in WELL_KNOWN_PATHS[LinkSource.SITEMAP]
                ]
            )
            remaining = max(0, self.max_links - len(manifest.links))
            links, reports, truncated = await self.fetch_sitemaps(
                seeds, follow_index=follow_sitemap_index, budget=remaining
            )
            manifest.links.extend(links)
            manifest.reports.extend(reports)
            manifest.truncated = manifest.truncated or truncated

        if LinkSource.LLMS in wanted:
            links, report = await self.fetch_llms(base)
            manifest.links.extend(links)
            manifest.reports.append(report)
            if include_llms_full:
                links, report = await self.fetch_llms(base, path="/llms-full.txt")
                manifest.links.extend(links)
                manifest.reports.append(report)

        if LinkSource.AI in wanted:
            ai_info, report = await self.fetch_ai(base)
            manifest.ai = ai_info
            manifest.reports.append(report)

        # De-duplicate across sources, keeping the first (richest) occurrence.
        deduped: list[DiscoveredLink] = []
        seen: set[str] = set()
        for link in manifest.links:
            if link.url in seen:
                continue
            seen.add(link.url)
            deduped.append(link)
        manifest.links = deduped[: self.max_links]
        if len(deduped) > self.max_links:
            manifest.truncated = True

        logger.debug(
            "discovery for %s found %d links (truncated=%s)",
            base,
            len(manifest.links),
            manifest.truncated,
        )
        return manifest
