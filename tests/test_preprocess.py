"""DOM/JSON/XML trimming — where most token spend is won or lost."""

from __future__ import annotations

import json

from scrapekit.models import ContentType
from scrapekit.parsers.preprocess import html_to_text, preprocess, trim_json, trim_xml


class TestHtmlToText:
    def test_keeps_the_content_that_matters(self, product_html):
        text = html_to_text(product_html)
        assert "Trail Runner GTX" in text
        assert "149.95" in text
        assert "Gore-Tex membrane" in text

    def test_drops_scripts_styles_and_markup(self, product_html):
        text = html_to_text(product_html)
        assert "dataLayer" not in text
        assert "color:#c00" not in text
        assert "<span" not in text
        assert "app.css" not in text

    def test_narrows_to_the_main_region(self, product_html):
        text = html_to_text(product_html)
        # <aside> and <footer> live outside <main> and should be gone.
        assert "You may also like" not in text
        assert "All rights reserved" not in text

    def test_substantially_reduces_size(self, product_html):
        assert len(html_to_text(product_html)) < len(product_html) / 2

    def test_keeps_links_when_asked(self, product_html):
        text = html_to_text(product_html, keep_links=True)
        assert "/docs/trail-runner-gtx.pdf" in text
        assert "/docs/trail-runner-gtx.pdf" not in html_to_text(product_html, keep_links=False)

    def test_does_not_narrow_when_the_main_region_is_trivial(self):
        html = (
            "<html><body><main></main>"
            "<div>" + "The real content lives here. " * 40 + "</div>"
            "</body></html>"
        )
        assert "The real content lives here." in html_to_text(html)

    def test_handles_empty_and_malformed_input(self):
        assert html_to_text("") == ""
        assert "hello" in html_to_text("<div><p>hello<div>unclosed")

    def test_collapses_whitespace(self):
        assert html_to_text("<p>a    b</p><p>c</p>") == "a b\nc"


class TestTrimJson:
    def test_compacts_without_losing_data(self, product_json):
        trimmed = trim_json(product_json)
        assert len(trimmed) < len(product_json)
        assert json.loads(trimmed)["product"]["title"] == "Trail Runner GTX"

    def test_truncates_long_arrays_with_an_explicit_marker(self):
        payload = json.dumps({"items": [{"i": i} for i in range(500)]})
        result = json.loads(trim_json(payload, max_items=10))
        assert len(result["items"]) == 11
        assert "490 more items omitted" in result["items"][-1]

    def test_caps_depth(self):
        deep: dict = {"v": 1}
        for _ in range(20):
            deep = {"n": deep}
        assert "..." in trim_json(json.dumps(deep), max_depth=3)

    def test_invalid_json_is_returned_unchanged(self):
        assert trim_json("{not json") == "{not json"


class TestTrimXml:
    def test_strips_comments_and_inter_element_whitespace(self, feed_xml):
        trimmed = trim_xml(feed_xml)
        assert "generated nightly" not in trimmed
        assert "Trail Runner GTX" in trimmed
        assert len(trimmed) < len(feed_xml)


class TestPreprocess:
    def test_dispatches_on_content_type(self, product_html, product_json, feed_xml):
        assert "<span" not in preprocess(product_html, ContentType.HTML)
        assert preprocess(product_json, ContentType.JSON).startswith("{")
        assert "generated nightly" not in preprocess(feed_xml, ContentType.XML)

    def test_truncation_is_marked_not_silent(self):
        """A cut-off field must not look merely absent to the extractor."""
        result = preprocess("x" * 5000, ContentType.TEXT, max_chars=100)
        assert "truncated" in result
        assert "4900 characters omitted" in result

    def test_no_truncation_when_disabled(self):
        assert len(preprocess("x" * 5000, ContentType.TEXT, max_chars=None)) == 5000

    def test_short_content_is_untouched(self):
        assert preprocess("hello", ContentType.TEXT, max_chars=100) == "hello"
