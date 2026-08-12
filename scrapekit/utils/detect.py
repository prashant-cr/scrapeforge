"""Content-type sniffing and generic block/challenge detection.

Everything here is data-driven: the signature lists below are the *only* place
where bot-management vendors are named, and they are matched generically. No
per-site special cases, and no attempt to solve a challenge — detection exists
so the fallback chain can escalate and, if all else fails, raise
:class:`~scrapekit.exceptions.ChallengeError`.
"""

from __future__ import annotations

import json
import re

from ..models import ContentType

__all__ = [
    "BLOCK_STATUS_CODES",
    "CHALLENGE_SIGNATURES",
    "classify_response",
    "detect_content_type",
    "looks_like_challenge",
    "sniff_body",
]

#: Statuses that *may* indicate a block. A 2xx never escalates on status alone;
#: these only escalate when paired with a challenge signature or a tiny body.
BLOCK_STATUS_CODES: frozenset[int] = frozenset({401, 403, 429, 503})

#: Substrings that identify a generic interstitial / challenge / block page.
#: Matched case-insensitively against the decoded body.
CHALLENGE_SIGNATURES: tuple[str, ...] = (
    # Generic interstitials
    "just a moment",
    "checking your browser",
    "enable javascript and cookies to continue",
    "verify you are human",
    "verifying you are human",
    "please enable cookies",
    "attention required!",
    "access denied",
    "request blocked",
    "unusual traffic",
    "are you a robot",
    # Vendor-neutral markers commonly present in challenge markup/scripts
    "cf-chl",
    "cf_chl_opt",
    "__cf_bm",
    "/cdn-cgi/challenge-platform",
    "captcha-delivery.com",
    "geo.captcha-delivery",
    "datadome",
    "_px_captcha",
    "px-captcha",
    "perimeterx",
    "incapsula incident id",
    "_incapsula_resource",
    "imperva",
    "distil_r_captcha",
    "akamai reference number",
    "reference #18.",
    "please verify you are a human",
    "g-recaptcha",
    "h-captcha",
    "turnstile",
)

#: Below this many bytes, a blocked-looking status is treated as a hard block
#: even without a signature match (empty/stub bodies are typical of WAF drops).
_TINY_BODY_BYTES = 1024

_HTML_HINTS = ("<!doctype html", "<html", "<head", "<body", "<div", "<span", "<p>")
_XML_DECL = re.compile(rb"^\s*<\?xml\b", re.IGNORECASE)
_HTML_DECL = re.compile(rb"^\s*<(?:!doctype\s+html|html)\b", re.IGNORECASE)


def detect_content_type(header_value: str | None, body: bytes | None = None) -> ContentType:
    """Resolve the content type from the ``Content-Type`` header, then the body.

    The header wins when it is unambiguous; otherwise the body is sniffed. This
    matters because block pages are frequently served as HTML from endpoints that
    normally return JSON.

    Args:
        header_value: Raw ``Content-Type`` header value, or ``None``.
        body: Response body used for sniffing when the header is unhelpful.

    Returns:
        The resolved :class:`~scrapekit.models.ContentType`.
    """
    mime = (header_value or "").split(";", 1)[0].strip().lower()

    if mime:
        if mime in ("application/json", "text/json") or mime.endswith("+json"):
            return ContentType.JSON
        if mime in ("text/html", "application/xhtml+xml"):
            return ContentType.HTML
        if mime in ("application/xml", "text/xml") or mime.endswith("+xml"):
            return ContentType.XML
        if mime.startswith("text/"):
            # text/plain is frequently used for JSON APIs; sniff to be sure.
            sniffed = sniff_body(body)
            return sniffed if sniffed is not ContentType.UNKNOWN else ContentType.TEXT
        if mime.startswith(("image/", "audio/", "video/", "font/")) or mime in (
            "application/octet-stream",
            "application/pdf",
            "application/zip",
        ):
            return ContentType.BINARY

    sniffed = sniff_body(body)
    return sniffed if sniffed is not ContentType.UNKNOWN else ContentType.UNKNOWN


def sniff_body(body: bytes | None) -> ContentType:
    """Guess the content type from the body alone.

    Returns :attr:`~scrapekit.models.ContentType.UNKNOWN` when nothing matches.
    """
    if not body:
        return ContentType.UNKNOWN

    head = body[:2048].lstrip()
    if not head:
        return ContentType.UNKNOWN

    if _HTML_DECL.match(head):
        return ContentType.HTML
    if _XML_DECL.match(head):
        return ContentType.XML

    if head[:1] in (b"{", b"["):
        try:
            json.loads(body.decode("utf-8", errors="strict"))
        except (ValueError, UnicodeDecodeError):
            pass
        else:
            return ContentType.JSON

    lowered = head.decode("utf-8", errors="replace").lower()
    if any(hint in lowered for hint in _HTML_HINTS):
        return ContentType.HTML
    if lowered.startswith("<"):
        return ContentType.XML

    if b"\x00" in body[:1024]:
        return ContentType.BINARY

    return ContentType.TEXT


def looks_like_challenge(text: str) -> str | None:
    """Return the matched challenge signature, or ``None`` if the body looks clean.

    Only the first portion of the body is scanned — challenge markers live in the
    head/top of the document, and scanning a multi-megabyte page is wasteful.
    """
    if not text:
        return None
    haystack = text[:20_000].lower()
    for signature in CHALLENGE_SIGNATURES:
        if signature in haystack:
            return signature
    return None


def classify_response(
    *,
    status_code: int,
    body: bytes,
    content_type: ContentType,
    expected_content_type: ContentType | None = None,
) -> tuple[bool, str | None]:
    """Decide whether a response is usable or the chain should escalate.

    Args:
        status_code: HTTP status of the response.
        body: Raw response body.
        content_type: Resolved content type of the response.
        expected_content_type: What the caller asked for, if anything.

    Returns:
        ``(usable, reason)``. When ``usable`` is ``False``, ``reason`` describes
        the trigger — either a matched signature, or a short code such as
        ``"status-403-empty-body"``.
    """
    text = body.decode("utf-8", errors="replace") if body else ""

    signature = looks_like_challenge(text)
    if signature:
        return False, signature

    if status_code in BLOCK_STATUS_CODES and len(body) < _TINY_BODY_BYTES:
        return False, f"status-{status_code}-empty-body"

    if status_code in BLOCK_STATUS_CODES and content_type is ContentType.HTML:
        return False, f"status-{status_code}-html"

    if (
        expected_content_type is not None
        and content_type is not expected_content_type
        and content_type in (ContentType.HTML, ContentType.UNKNOWN)
    ):
        return False, f"expected-{expected_content_type.value}-got-{content_type.value}"

    if 200 <= status_code < 300:
        return True, None

    if status_code in BLOCK_STATUS_CODES:
        return False, f"status-{status_code}"

    # 3xx (unfollowed), 404, 500... are real answers from the origin, not blocks.
    # Hand them back so the caller can decide; escalating would not help.
    return True, None
