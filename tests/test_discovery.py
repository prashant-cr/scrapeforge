"""Discovery orchestration: fetching, sitemap recursion, limits, and reporting.

All HTTP is mocked. What matters here is restraint — indexes nest, cycles
happen, and sitemaps are allowed to be enormous — so most of these assert that
discovery *stops* when it should, and says so.
"""

from __future__ import annotations

import gzip

import httpx
import pytest
import respx

from scrapeforge import Scraper
from scrapeforge.discovery import LinkSource, SiteDiscoverer
from scrapeforge.exceptions import ConfigError

BASE = "https://example.com"


def sitemap_index(*urls: str) -> str:
    entries = "".join(f"<sitemap><loc>{u}</loc></sitemap>" for u in urls)
    return (
        '<?xml version="1.0"?><sitemapindex '
        f'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{entries}</sitemapindex>'
    )


def urlset(*urls: str) -> str:
    entries = "".join(f"<url><loc>{u}</loc></url>" for u in urls)
    return (
        '<?xml version="1.0"?><urlset '
        f'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{entries}</urlset>'
    )


def missing(*paths: str) -> None:
    """Register 404s, which is what most sites return for these files."""
    for path in paths:
        respx.get(f"{BASE}{path}").mock(return_value=httpx.Response(404))


@pytest.fixture
def scraper() -> Scraper:
    return Scraper(
        strategies=["http"], respect_robots=False, min_delay=0, max_delay=0, max_retries=0
    )


class TestSourceCombination:
    @respx.mock
    async def test_reads_all_four_sources(self, scraper):
        respx.get(f"{BASE}/robots.txt").mock(
            return_value=httpx.Response(200, text=f"Sitemap: {BASE}/sitemap.xml")
        )
        respx.get(f"{BASE}/sitemap.xml").mock(
            return_value=httpx.Response(200, text=urlset(f"{BASE}/a", f"{BASE}/b"))
        )
        respx.get(f"{BASE}/llms.txt").mock(
            return_value=httpx.Response(200, text="## Docs\n- [Guide](/guide): How to")
        )
        respx.get(f"{BASE}/ai.txt").mock(
            return_value=httpx.Response(200, text="User-Agent: *\nDisallow: /private/")
        )

        manifest = await scraper.discover(BASE)

        assert manifest.base_url == BASE
        assert f"{BASE}/a" in manifest.urls
        assert f"{BASE}/guide" in manifest.urls
        assert manifest.robots.sitemaps == [f"{BASE}/sitemap.xml"]
        assert manifest.ai.disallow == ["/private/"]
        assert all(r.ok for r in manifest.reports)

    @respx.mock
    async def test_llms_metadata_survives_into_the_manifest(self, scraper):
        missing(
            "/robots.txt", "/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml", "/ai.txt"
        )
        respx.get(f"{BASE}/llms.txt").mock(
            return_value=httpx.Response(
                200, text="## Docs\n- [Guide](https://example.com/guide): How to use it"
            )
        )

        manifest = await scraper.discover(BASE)
        link = manifest.by_source("llms")[0]

        assert link.title == "Guide"
        assert link.description == "How to use it"
        assert link.section == "Docs"

    @respx.mock
    async def test_sources_can_be_selected(self, scraper):
        robots = respx.get(f"{BASE}/robots.txt").mock(
            return_value=httpx.Response(200, text="User-agent: *")
        )
        llms = respx.get(f"{BASE}/llms.txt").mock(return_value=httpx.Response(404))

        await scraper.discover(BASE, sources=["robots"])

        assert robots.called
        assert not llms.called

    @respx.mock
    async def test_missing_files_are_reported_not_raised(self, scraper):
        """Most sites publish none of these; absence is data, not an error."""
        missing(
            "/robots.txt",
            "/sitemap.xml",
            "/sitemap_index.xml",
            "/sitemap-index.xml",
            "/llms.txt",
            "/ai.txt",
        )

        manifest = await scraper.discover(BASE)

        assert manifest.links == []
        assert manifest.report_for("llms").status == "not_found"
        assert manifest.robots is None

    @respx.mock
    async def test_a_broken_source_does_not_stop_the_others(self, scraper):
        respx.get(f"{BASE}/robots.txt").mock(return_value=httpx.Response(500))
        missing("/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml", "/ai.txt")
        respx.get(f"{BASE}/llms.txt").mock(
            return_value=httpx.Response(200, text=f"- [A]({BASE}/a)")
        )

        manifest = await scraper.discover(BASE)

        assert manifest.report_for("robots").status == "error"
        assert f"{BASE}/a" in manifest.urls

    @respx.mock
    async def test_a_malformed_sitemap_is_reported_as_an_error(self, scraper):
        respx.get(f"{BASE}/robots.txt").mock(
            return_value=httpx.Response(200, text=f"Sitemap: {BASE}/sitemap.xml")
        )
        respx.get(f"{BASE}/sitemap.xml").mock(
            return_value=httpx.Response(200, text="<urlset><url>")
        )
        missing("/llms.txt", "/ai.txt")

        manifest = await scraper.discover(BASE)
        report = next(r for r in manifest.reports if r.source is LinkSource.SITEMAP)

        assert report.status == "error"
        assert "not valid XML" in report.detail


class TestSitemapDiscovery:
    @respx.mock
    async def test_prefers_the_sitemap_robots_advertises(self, scraper):
        respx.get(f"{BASE}/robots.txt").mock(
            return_value=httpx.Response(200, text=f"Sitemap: {BASE}/custom-sitemap.xml")
        )
        custom = respx.get(f"{BASE}/custom-sitemap.xml").mock(
            return_value=httpx.Response(200, text=urlset(f"{BASE}/a"))
        )
        guessed = respx.get(f"{BASE}/sitemap.xml").mock(
            return_value=httpx.Response(200, text=urlset())
        )
        missing("/llms.txt", "/ai.txt")

        await scraper.discover(BASE)

        assert custom.called
        assert not guessed.called, "should not guess when robots.txt names a sitemap"

    @respx.mock
    async def test_falls_back_to_conventional_paths(self, scraper):
        missing("/robots.txt", "/llms.txt", "/ai.txt")
        guessed = respx.get(f"{BASE}/sitemap.xml").mock(
            return_value=httpx.Response(200, text=urlset(f"{BASE}/a"))
        )
        missing("/sitemap_index.xml", "/sitemap-index.xml")

        manifest = await scraper.discover(BASE)

        assert guessed.called
        assert f"{BASE}/a" in manifest.urls

    @respx.mock
    async def test_follows_a_sitemap_index(self, scraper):
        respx.get(f"{BASE}/robots.txt").mock(
            return_value=httpx.Response(200, text=f"Sitemap: {BASE}/sitemap.xml")
        )
        respx.get(f"{BASE}/sitemap.xml").mock(
            return_value=httpx.Response(200, text=sitemap_index(f"{BASE}/child.xml"))
        )
        respx.get(f"{BASE}/child.xml").mock(
            return_value=httpx.Response(200, text=urlset(f"{BASE}/deep-page"))
        )
        missing("/llms.txt", "/ai.txt")

        manifest = await scraper.discover(BASE)

        assert f"{BASE}/deep-page" in manifest.page_urls
        assert f"{BASE}/child.xml" not in manifest.page_urls  # it is a sitemap, not a page

    @respx.mock
    async def test_index_can_be_left_unfollowed(self, scraper):
        respx.get(f"{BASE}/robots.txt").mock(
            return_value=httpx.Response(200, text=f"Sitemap: {BASE}/sitemap.xml")
        )
        respx.get(f"{BASE}/sitemap.xml").mock(
            return_value=httpx.Response(200, text=sitemap_index(f"{BASE}/child.xml"))
        )
        child = respx.get(f"{BASE}/child.xml").mock(return_value=httpx.Response(200, text=urlset()))
        missing("/llms.txt", "/ai.txt")

        manifest = await scraper.discover(BASE, follow_sitemap_index=False)

        assert not child.called
        assert f"{BASE}/child.xml" in manifest.urls  # recorded, just not walked

    @respx.mock
    async def test_gzipped_sitemaps_are_decompressed(self, scraper):
        respx.get(f"{BASE}/robots.txt").mock(
            return_value=httpx.Response(200, text=f"Sitemap: {BASE}/sitemap.xml.gz")
        )
        respx.get(f"{BASE}/sitemap.xml.gz").mock(
            return_value=httpx.Response(200, content=gzip.compress(urlset(f"{BASE}/a").encode()))
        )
        missing("/llms.txt", "/ai.txt")

        manifest = await scraper.discover(BASE)

        assert f"{BASE}/a" in manifest.urls


class TestLimits:
    @respx.mock
    async def test_a_cycle_between_indexes_terminates(self, scraper):
        """Two indexes pointing at each other must not loop forever."""
        respx.get(f"{BASE}/robots.txt").mock(
            return_value=httpx.Response(200, text=f"Sitemap: {BASE}/a.xml")
        )
        respx.get(f"{BASE}/a.xml").mock(
            return_value=httpx.Response(200, text=sitemap_index(f"{BASE}/b.xml"))
        )
        b = respx.get(f"{BASE}/b.xml").mock(
            return_value=httpx.Response(200, text=sitemap_index(f"{BASE}/a.xml"))
        )
        missing("/llms.txt", "/ai.txt")

        manifest = await scraper.discover(BASE)

        assert b.call_count == 1, "each sitemap should be fetched at most once"
        assert len(manifest.links) < 10

    @respx.mock
    async def test_depth_limit_stops_the_descent(self, scraper):
        respx.get(f"{BASE}/robots.txt").mock(
            return_value=httpx.Response(200, text=f"Sitemap: {BASE}/d0.xml")
        )
        for depth in range(5):
            respx.get(f"{BASE}/d{depth}.xml").mock(
                return_value=httpx.Response(200, text=sitemap_index(f"{BASE}/d{depth + 1}.xml"))
            )
        deepest = respx.get(f"{BASE}/d4.xml")
        missing("/llms.txt", "/ai.txt")

        scraper.config.max_sitemap_depth = 1
        scraper._discoverer = None  # rebuild with the new limit
        manifest = await scraper.discover(BASE)

        assert not deepest.called
        assert manifest.truncated is True

    @respx.mock
    async def test_document_limit_is_enforced_and_reported(self, scraper):
        respx.get(f"{BASE}/robots.txt").mock(
            return_value=httpx.Response(200, text=f"Sitemap: {BASE}/index.xml")
        )
        children = [f"{BASE}/c{i}.xml" for i in range(10)]
        respx.get(f"{BASE}/index.xml").mock(
            return_value=httpx.Response(200, text=sitemap_index(*children))
        )
        for child in children:
            respx.get(child).mock(return_value=httpx.Response(200, text=urlset(f"{child}#p")))
        missing("/llms.txt", "/ai.txt")

        scraper.config.max_sitemap_documents = 3
        scraper._discoverer = None
        manifest = await scraper.discover(BASE)

        assert manifest.truncated is True
        assert any(r.status == "skipped" for r in manifest.reports)

    @respx.mock
    async def test_link_limit_truncates_and_says_so(self, scraper):
        respx.get(f"{BASE}/robots.txt").mock(
            return_value=httpx.Response(200, text=f"Sitemap: {BASE}/sitemap.xml")
        )
        respx.get(f"{BASE}/sitemap.xml").mock(
            return_value=httpx.Response(200, text=urlset(*[f"{BASE}/p{i}" for i in range(100)]))
        )
        missing("/llms.txt", "/ai.txt")

        scraper.config.max_discovered_links = 10
        scraper._discoverer = None
        manifest = await scraper.discover(BASE)

        assert len(manifest.links) <= 10
        assert manifest.truncated is True

    @respx.mock
    async def test_oversized_documents_are_skipped(self, scraper):
        respx.get(f"{BASE}/robots.txt").mock(
            return_value=httpx.Response(200, text=f"Sitemap: {BASE}/huge.xml")
        )
        respx.get(f"{BASE}/huge.xml").mock(
            return_value=httpx.Response(200, text=urlset(*[f"{BASE}/p{i}" for i in range(200)]))
        )
        missing("/llms.txt", "/ai.txt")

        scraper._discoverer = SiteDiscoverer(scraper.fetch, max_document_bytes=100)
        manifest = await scraper.discover(BASE)

        report = next(r for r in manifest.reports if "huge" in r.url)
        assert report.status == "error"
        assert "over the" in report.detail

    def test_invalid_limits_are_rejected(self, scraper):
        with pytest.raises(ConfigError):
            SiteDiscoverer(scraper.fetch, max_links=0)


class TestBehaviour:
    @respx.mock
    async def test_discovery_never_fetches_a_discovered_page(self, scraper):
        """Discovery lists URLs; it does not crawl them."""
        respx.get(f"{BASE}/robots.txt").mock(
            return_value=httpx.Response(200, text=f"Sitemap: {BASE}/sitemap.xml")
        )
        respx.get(f"{BASE}/sitemap.xml").mock(
            return_value=httpx.Response(200, text=urlset(f"{BASE}/page-one"))
        )
        page = respx.get(f"{BASE}/page-one").mock(return_value=httpx.Response(200, text="hi"))
        missing("/llms.txt", "/ai.txt")

        await scraper.discover(BASE)

        assert not page.called

    @respx.mock
    async def test_well_known_files_bypass_robots(self, scraper):
        """A robots.txt that forbids reading robots.txt is incoherent."""
        respx.get(f"{BASE}/robots.txt").mock(
            return_value=httpx.Response(200, text="User-agent: *\nDisallow: /")
        )
        missing("/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml", "/ai.txt")
        llms = respx.get(f"{BASE}/llms.txt").mock(
            return_value=httpx.Response(200, text=f"- [A]({BASE}/a)")
        )

        polite = Scraper(strategies=["http"], respect_robots=True, min_delay=0, max_delay=0)
        manifest = await polite.discover(BASE)

        assert llms.called
        assert f"{BASE}/a" in manifest.urls

    @respx.mock
    async def test_any_url_on_the_site_works_not_just_the_origin(self, scraper):
        robots = respx.get(f"{BASE}/robots.txt").mock(return_value=httpx.Response(404))
        missing("/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml", "/llms.txt", "/ai.txt")

        manifest = await scraper.discover(f"{BASE}/deep/page?q=1#frag")

        assert robots.called
        assert manifest.base_url == BASE

    @respx.mock
    async def test_links_are_deduplicated_across_sources(self, scraper):
        shared = f"{BASE}/shared"
        respx.get(f"{BASE}/robots.txt").mock(
            return_value=httpx.Response(200, text=f"Sitemap: {BASE}/sitemap.xml")
        )
        respx.get(f"{BASE}/sitemap.xml").mock(return_value=httpx.Response(200, text=urlset(shared)))
        respx.get(f"{BASE}/llms.txt").mock(
            return_value=httpx.Response(200, text=f"- [S]({shared})")
        )
        missing("/ai.txt")

        manifest = await scraper.discover(BASE)

        assert manifest.urls.count(shared) == 1

    def test_sync_mirror(self):
        """Must NOT be async: the sync mirrors refuse to nest inside a live loop."""
        with respx.mock:
            respx.get(f"{BASE}/robots.txt").mock(
                return_value=httpx.Response(200, text=f"Sitemap: {BASE}/s.xml")
            )
            respx.get(f"{BASE}/s.xml").mock(
                return_value=httpx.Response(200, text=urlset(f"{BASE}/a"))
            )
            missing("/llms.txt", "/ai.txt")
            s = Scraper(strategies=["http"], respect_robots=False, min_delay=0, max_delay=0)
            manifest = s.discover_sync(BASE)
        assert f"{BASE}/a" in manifest.urls
