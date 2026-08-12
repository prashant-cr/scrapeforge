"""The four discovery parsers, against committed fixtures.

These are pure functions over text/bytes, so every case here is offline and
deterministic. Fetching, recursion, and limits are covered in
``test_discovery.py``.
"""

from __future__ import annotations

import gzip
from pathlib import Path

import pytest

from scrapeforge.discovery import (
    LinkSource,
    parse_ai_txt,
    parse_llms_txt,
    parse_robots_txt,
    parse_sitemap,
)
from scrapeforge.exceptions import ParseError

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def robots_txt() -> str:
    return (FIXTURES / "robots.txt").read_text()


@pytest.fixture
def sitemap_index() -> bytes:
    return (FIXTURES / "sitemap_index.xml").read_bytes()


@pytest.fixture
def sitemap_urlset() -> bytes:
    return (FIXTURES / "sitemap_urlset.xml").read_bytes()


@pytest.fixture
def llms_txt() -> str:
    return (FIXTURES / "llms.txt").read_text()


@pytest.fixture
def ai_txt() -> str:
    return (FIXTURES / "ai.txt").read_text()


class TestRobotsTxt:
    def test_extracts_sitemap_directives(self, robots_txt):
        info = parse_robots_txt(robots_txt, url="https://example.com/robots.txt")
        assert info.sitemaps == [
            "https://example.com/sitemap.xml",
            "https://example.com/relative-sitemap.xml",  # resolved against the base
        ]

    def test_sitemaps_are_site_wide_not_agent_scoped(self, robots_txt):
        """The Sitemap lines sit after a BadBot group; they still belong to the site."""
        info = parse_robots_txt(
            robots_txt, url="https://example.com/robots.txt", user_agent="SomeOtherBot"
        )
        assert len(info.sitemaps) == 2

    def test_extracts_rules_for_the_wildcard_agent(self, robots_txt):
        info = parse_robots_txt(robots_txt)
        assert info.disallow == ["/private/", "/tmp/"]
        assert info.allow == ["/private/public-note"]
        assert info.crawl_delay == 2.0

    def test_named_agent_gets_its_own_rules(self, robots_txt):
        info = parse_robots_txt(robots_txt, user_agent="BadBot")
        assert info.disallow == ["/"]

    def test_unknown_agent_falls_back_to_wildcard(self, robots_txt):
        info = parse_robots_txt(robots_txt, user_agent="NeverHeardOfIt")
        assert info.disallow == ["/private/", "/tmp/"]

    def test_records_every_declared_agent(self, robots_txt):
        assert parse_robots_txt(robots_txt).user_agents == ["*", "BadBot"]

    def test_consecutive_user_agent_lines_share_one_rule_block(self):
        """RFC 9309: stacked User-agent lines form a single group.

        Getting this wrong silently drops the rules for every agent in the block
        except the last — found against python.org, whose file stacks three.
        """
        text = "User-agent: HTTrack\nUser-agent: puf\nUser-agent: MSIECrawler\nDisallow: /\n"
        for agent in ("HTTrack", "puf", "MSIECrawler"):
            assert parse_robots_txt(text, user_agent=agent).disallow == ["/"], agent

    def test_a_rule_line_closes_the_agent_list(self):
        """After a rule, the next User-agent starts a new group rather than joining."""
        text = "User-agent: A\nDisallow: /a\nUser-agent: B\nDisallow: /b\n"
        assert parse_robots_txt(text, user_agent="A").disallow == ["/a"]
        assert parse_robots_txt(text, user_agent="B").disallow == ["/b"]

    def test_no_wildcard_group_means_no_rules_for_a_generic_agent(self):
        """A file naming only specific bots does not constrain everyone else."""
        text = "User-agent: BadBot\nDisallow: /\n"
        info = parse_robots_txt(text, user_agent="SomeOtherBot")
        assert info.disallow == []
        assert info.allow == []

    def test_crawl_delay_applies_to_the_whole_group(self):
        text = "User-agent: A\nUser-agent: B\nCrawl-delay: 5\n"
        assert parse_robots_txt(text, user_agent="A").crawl_delay == 5.0
        assert parse_robots_txt(text, user_agent="B").crawl_delay == 5.0

    def test_a_malformed_crawl_delay_is_ignored_not_fatal(self):
        info = parse_robots_txt("User-agent: *\nCrawl-delay: soon\nDisallow: /x")
        assert info.crawl_delay is None
        assert info.disallow == ["/x"]

    def test_comments_are_stripped(self):
        info = parse_robots_txt("Sitemap: https://e.com/s.xml  # the sitemap")
        assert info.sitemaps == ["https://e.com/s.xml"]

    def test_empty_file(self):
        info = parse_robots_txt("")
        assert info.sitemaps == [] and info.disallow == []

    def test_relative_sitemap_is_dropped_without_a_base_url(self):
        """Nothing to resolve against, so it is omitted rather than returned broken."""
        info = parse_robots_txt("Sitemap: /s.xml")
        assert info.sitemaps == []

    def test_non_http_sitemap_is_rejected(self):
        info = parse_robots_txt("Sitemap: ftp://example.com/s.xml")
        assert info.sitemaps == []


class TestSitemap:
    def test_parses_a_urlset_with_metadata(self, sitemap_urlset):
        links = parse_sitemap(sitemap_urlset, url="https://example.com/sitemap.xml")
        first = links[0]
        assert first.url == "https://example.com/"
        assert first.source is LinkSource.SITEMAP
        assert first.lastmod == "2026-08-10"
        assert first.changefreq == "daily"
        assert first.priority == 1.0
        assert first.is_sitemap is False
        assert first.found_in == "https://example.com/sitemap.xml"

    def test_resolves_relative_locs(self, sitemap_urlset):
        urls = [link.url for link in parse_sitemap(sitemap_urlset, url="https://example.com/s.xml")]
        assert "https://example.com/relative-page" in urls

    def test_deduplicates_within_a_document(self, sitemap_urlset):
        urls = [link.url for link in parse_sitemap(sitemap_urlset, url="https://example.com/s.xml")]
        assert urls.count("https://example.com/about") == 1

    def test_index_entries_are_flagged_as_sitemaps(self, sitemap_index):
        links = parse_sitemap(sitemap_index, url="https://example.com/sitemap.xml")
        assert len(links) == 2
        assert all(link.is_sitemap for link in links)
        assert links[0].url == "https://example.com/sitemap-pages.xml"

    def test_handles_gzipped_sitemaps(self, sitemap_urlset):
        """`.xml.gz` is the common case for large sitemaps."""
        links = parse_sitemap(gzip.compress(sitemap_urlset), url="https://example.com/s.xml.gz")
        assert links and links[0].url == "https://example.com/"

    def test_handles_documents_without_a_namespace(self):
        body = b"<urlset><url><loc>https://example.com/x</loc></url></urlset>"
        assert parse_sitemap(body)[0].url == "https://example.com/x"

    def test_empty_document(self):
        assert parse_sitemap(b"   ") == []

    def test_malformed_xml_raises_parse_error(self):
        with pytest.raises(ParseError, match="not valid XML"):
            parse_sitemap(b"<urlset><url>")

    def test_wrong_root_element_raises(self):
        with pytest.raises(ParseError, match="unexpected sitemap root"):
            parse_sitemap(b"<html><body>nope</body></html>")

    def test_broken_gzip_raises_parse_error(self):
        with pytest.raises(ParseError, match="would not decompress"):
            parse_sitemap(b"\x1f\x8b" + b"garbage")

    @pytest.mark.parametrize(
        "payload",
        [
            b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x "boom">]><urlset/>',
            b'<!DOCTYPE foo SYSTEM "file:///etc/passwd"><urlset/>',
        ],
    )
    def test_refuses_documents_declaring_entities(self, payload):
        """XXE and billion-laughs both arrive as a DTD; reject rather than parse."""
        with pytest.raises(ParseError, match="DTD or entity"):
            parse_sitemap(payload)


class TestLlmsTxt:
    def test_extracts_markdown_links_with_titles(self, llms_txt):
        links = parse_llms_txt(llms_txt, url="https://example.com/llms.txt")
        by_url = {link.url: link for link in links}
        start = by_url["https://example.com/docs/start"]
        assert start.title == "Getting started"
        assert start.description == "Install and first request"
        assert start.section == "Docs"
        assert start.source is LinkSource.LLMS

    def test_resolves_relative_links(self, llms_txt):
        urls = [link.url for link in parse_llms_txt(llms_txt, url="https://example.com/llms.txt")]
        assert "https://example.com/docs/api" in urls

    def test_tracks_the_section_heading(self, llms_txt):
        links = parse_llms_txt(llms_txt, url="https://example.com/llms.txt")
        changelog = next(link for link in links if link.url.endswith("/changelog"))
        assert changelog.section == "Optional"

    def test_ignores_links_inside_code_fences(self, llms_txt):
        urls = [link.url for link in parse_llms_txt(llms_txt, url="https://example.com/llms.txt")]
        assert not any("inside-a-code-fence" in u for u in urls)

    def test_picks_up_bare_urls_in_prose(self, llms_txt):
        urls = [link.url for link in parse_llms_txt(llms_txt, url="https://example.com/llms.txt")]
        assert "https://example.com/prose-link" in urls

    def test_deduplicates(self, llms_txt):
        urls = [link.url for link in parse_llms_txt(llms_txt, url="https://example.com/llms.txt")]
        assert urls.count("https://example.com/docs/start") == 1

    def test_empty_file(self):
        assert parse_llms_txt("") == []

    def test_the_h1_title_is_not_treated_as_a_section(self, llms_txt):
        links = parse_llms_txt(llms_txt, url="https://example.com/llms.txt")
        prose = next(link for link in links if link.url.endswith("/prose-link"))
        assert prose.section is None


class TestAiTxt:
    def test_extracts_wildcard_rules(self, ai_txt):
        info = parse_ai_txt(ai_txt, url="https://example.com/ai.txt")
        assert info.disallow == ["/members/"]
        assert info.allow == ["/blog/"]

    def test_named_agent_rules(self, ai_txt):
        info = parse_ai_txt(ai_txt, user_agent="GPTBot")
        assert info.disallow == ["/"]
        assert info.disallows_everything is True

    def test_wildcard_agent_does_not_disallow_everything(self, ai_txt):
        assert parse_ai_txt(ai_txt).disallows_everything is False

    def test_unmodelled_directives_are_kept_not_dropped(self, ai_txt):
        """There is no single ratified ai.txt spec; keep what we don't model."""
        info = parse_ai_txt(ai_txt)
        assert info.directives["disallowaitraining"] == ["/"]
        assert info.directives["contact"] == ["ai@example.com"]

    def test_records_declared_agents(self, ai_txt):
        assert parse_ai_txt(ai_txt).user_agents == ["*", "GPTBot"]

    def test_empty_file(self):
        info = parse_ai_txt("")
        assert info.allow == [] and info.disallow == [] and info.directives == {}
