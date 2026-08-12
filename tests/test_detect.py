"""Content sniffing and challenge-signature matching."""

from __future__ import annotations

import pytest

from scrapeforge.models import ContentType
from scrapeforge.utils.detect import (
    classify_response,
    detect_content_type,
    looks_like_challenge,
    sniff_body,
)


class TestDetectContentType:
    @pytest.mark.parametrize(
        ("header", "expected"),
        [
            ("application/json", ContentType.JSON),
            ("application/json; charset=utf-8", ContentType.JSON),
            ("application/ld+json", ContentType.JSON),
            ("text/html; charset=iso-8859-1", ContentType.HTML),
            ("application/xhtml+xml", ContentType.HTML),
            ("application/xml", ContentType.XML),
            ("application/rss+xml", ContentType.XML),
            ("image/png", ContentType.BINARY),
            ("application/pdf", ContentType.BINARY),
        ],
    )
    def test_header_wins_when_unambiguous(self, header, expected):
        assert detect_content_type(header, b"") is expected

    def test_text_plain_is_sniffed(self):
        # JSON APIs commonly mislabel their responses as text/plain.
        assert detect_content_type("text/plain", b'{"a": 1}') is ContentType.JSON
        assert detect_content_type("text/plain", b"hello world") is ContentType.TEXT

    def test_falls_back_to_sniffing_without_a_header(self, product_html, product_json):
        assert detect_content_type(None, product_html.encode()) is ContentType.HTML
        assert detect_content_type(None, product_json.encode()) is ContentType.JSON

    def test_empty_body_and_no_header_is_unknown(self):
        assert detect_content_type(None, b"") is ContentType.UNKNOWN


class TestSniffBody:
    def test_html_doctype(self):
        assert sniff_body(b"<!DOCTYPE html><html>") is ContentType.HTML

    def test_html_without_doctype(self):
        assert sniff_body(b"  <html><body><div>hi</div>") is ContentType.HTML

    def test_xml_declaration(self, feed_xml):
        assert sniff_body(feed_xml.encode()) is ContentType.XML

    def test_json_object_and_array(self):
        assert sniff_body(b'{"a": 1}') is ContentType.JSON
        assert sniff_body(b"[1, 2, 3]") is ContentType.JSON

    def test_json_shaped_but_invalid_is_not_json(self):
        assert sniff_body(b"{not valid json") is not ContentType.JSON

    def test_binary_with_null_bytes(self):
        assert sniff_body(b"\x89PNG\r\n\x1a\n\x00\x00") is ContentType.BINARY


class TestLooksLikeChallenge:
    def test_matches_interstitial_fixture(self, challenge_html):
        assert looks_like_challenge(challenge_html) == "just a moment"

    @pytest.mark.parametrize(
        "body",
        [
            "<html>Checking your browser before accessing</html>",
            "<div>Verify you are human</div>",
            '<script src="/cdn-cgi/challenge-platform/x"></script>',
            "<html>Access Denied</html>",
            "<div class='g-recaptcha'></div>",
            "Incapsula incident ID: 123-456",
        ],
    )
    def test_matches_generic_markers(self, body):
        assert looks_like_challenge(body) is not None

    def test_clean_page_does_not_match(self, product_html):
        assert looks_like_challenge(product_html) is None

    def test_empty_body(self):
        assert looks_like_challenge("") is None


class TestClassifyResponse:
    def test_clean_200_is_usable(self, product_html):
        usable, reason = classify_response(
            status_code=200, body=product_html.encode(), content_type=ContentType.HTML
        )
        assert usable is True
        assert reason is None

    def test_challenge_page_with_200_still_escalates(self, challenge_html):
        # Challenge pages are frequently served with a 200; status alone is not enough.
        usable, reason = classify_response(
            status_code=200, body=challenge_html.encode(), content_type=ContentType.HTML
        )
        assert usable is False
        assert reason == "just a moment"

    @pytest.mark.parametrize("status", [401, 403, 429, 503])
    def test_block_status_with_tiny_body(self, status):
        usable, reason = classify_response(
            status_code=status, body=b"denied", content_type=ContentType.TEXT
        )
        assert usable is False
        assert reason == f"status-{status}-empty-body"

    def test_content_type_mismatch_escalates(self, challenge_html):
        usable, reason = classify_response(
            status_code=200,
            body=b"<html><body>a login wall</body></html>",
            content_type=ContentType.HTML,
            expected_content_type=ContentType.JSON,
        )
        assert usable is False
        assert reason == "expected-json-got-html"

    def test_matching_expected_type_is_usable(self):
        usable, _ = classify_response(
            status_code=200,
            body=b'{"ok": true}',
            content_type=ContentType.JSON,
            expected_content_type=ContentType.JSON,
        )
        assert usable is True

    def test_404_is_a_real_answer_not_a_block(self):
        # Escalating to a browser will not turn a 404 into a 200.
        usable, reason = classify_response(
            status_code=404,
            body=b"<html><body>Not found</body></html>" + b"x" * 2000,
            content_type=ContentType.HTML,
        )
        assert usable is True
        assert reason is None

    def test_500_is_a_real_answer_not_a_block(self):
        usable, _ = classify_response(
            status_code=500, body=b"internal error", content_type=ContentType.TEXT
        )
        assert usable is True
