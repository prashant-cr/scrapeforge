"""Content trimming for token economy.

Raw pages are mostly boilerplate: scripts, styles, navigation, footers, tracking
pixels. Sending them to an LLM is slow and expensive, and the noise measurably
hurts extraction quality. This module reduces a document to the text that could
plausibly contain the requested fields.

Uses ``selectolax`` (a Lexbor binding) when available, with a regex fallback so a
broken or missing native extension degrades output quality rather than crashing.
"""

from __future__ import annotations

import json
import re

from ..models import ContentType

__all__ = ["html_to_text", "preprocess", "trim_json", "trim_xml"]

#: Elements whose contents never carry extractable data.
_DROP_TAGS = (
    "script",
    "style",
    "noscript",
    "template",
    "svg",
    "canvas",
    "iframe",
    "link",
    "meta",
    "head",
)

#: Elements that are usually chrome. Dropped only when the document still has
#: substantial content without them.
_BOILERPLATE_TAGS = ("nav", "footer", "aside", "form")

#: Selectors tried in order to find the main content region.
_MAIN_SELECTORS = ("main", "article", "[role=main]", "#content", "#main", ".content", ".main")

_SCRIPT_RE = re.compile(rb"<(script|style|noscript|template)\b.*?</\1>", re.IGNORECASE | re.DOTALL)
_COMMENT_RE = re.compile(rb"<!--.*?-->", re.DOTALL)
_TAG_RE = re.compile(rb"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_BLANKLINES_RE = re.compile(r"\n{3,}")


def _collapse(text: str) -> str:
    """Normalize whitespace without destroying line structure."""
    text = _WS_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    return _BLANKLINES_RE.sub("\n\n", text).strip()


def _html_to_text_fallback(html: str) -> str:
    raw = html.encode("utf-8", errors="replace")
    raw = _COMMENT_RE.sub(b" ", raw)
    raw = _SCRIPT_RE.sub(b" ", raw)
    raw = _TAG_RE.sub(b"\n", raw)
    import html as html_module

    return _collapse(html_module.unescape(raw.decode("utf-8", errors="replace")))


def html_to_text(
    html: str,
    *,
    main_content_only: bool = True,
    keep_links: bool = False,
) -> str:
    """Reduce an HTML document to extractable text.

    Args:
        html: The raw HTML document.
        main_content_only: Try to narrow to ``<main>``/``<article>``/etc. before
            extracting. Falls back to the full body when no such region exists or
            the candidate is suspiciously small.
        keep_links: Append ``href`` values inline as ``text (url)``. Useful when
            the schema has URL fields; noisy otherwise.

    Returns:
        Cleaned, whitespace-normalized text.
    """
    if not html:
        return ""

    try:
        from selectolax.parser import HTMLParser
    except ImportError:  # pragma: no cover - selectolax is a core dependency
        return _html_to_text_fallback(html)

    try:
        tree = HTMLParser(html)
    except Exception:  # pragma: no cover - malformed input
        return _html_to_text_fallback(html)

    tree.strip_tags(list(_DROP_TAGS))

    root = tree.body or tree.root
    if root is None:
        return _html_to_text_fallback(html)

    node = root
    if main_content_only:
        full_len = len(root.text(separator=" ", strip=True) or "")
        for selector in _MAIN_SELECTORS:
            candidate = tree.css_first(selector)
            if candidate is None:
                continue
            candidate_len = len(candidate.text(separator=" ", strip=True) or "")
            # Only narrow when the candidate holds most of the page's text;
            # otherwise we would drop the very content we were asked to extract.
            if candidate_len >= max(200, full_len * 0.3):
                node = candidate
                break

    if node is root:
        # Drop chrome only when we could not isolate a main region.
        for tag in _BOILERPLATE_TAGS:
            for element in node.css(tag):
                element.decompose()

    if keep_links:
        for anchor in node.css("a[href]"):
            href = anchor.attributes.get("href") or ""
            label = (anchor.text(strip=True) or "").strip()
            if href and label and not href.startswith("javascript:"):
                anchor.replace_with(f"{label} ({href})")

    return _collapse(node.text(separator="\n", strip=True) or "")


def trim_json(payload: str, *, max_items: int = 50, max_depth: int = 8) -> str:
    """Re-serialize JSON compactly, truncating very long arrays.

    Huge result sets blow the context window while adding nothing — the schema's
    fields are almost always present in the first handful of items.

    Args:
        payload: Raw JSON text.
        max_items: Keep at most this many elements per array.
        max_depth: Replace values nested deeper than this with ``"..."``.

    Returns:
        Compact JSON text, or the original string if it does not parse.
    """
    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        return payload

    def _walk(value: object, depth: int) -> object:
        if depth > max_depth:
            return "..."
        if isinstance(value, dict):
            return {k: _walk(v, depth + 1) for k, v in value.items()}
        if isinstance(value, list):
            trimmed = [_walk(v, depth + 1) for v in value[:max_items]]
            if len(value) > max_items:
                trimmed.append(f"...({len(value) - max_items} more items omitted)")
            return trimmed
        return value

    return json.dumps(_walk(data, 0), ensure_ascii=False, separators=(",", ":"))


def trim_xml(payload: str) -> str:
    """Strip XML comments and collapse whitespace between elements."""
    if not payload:
        return ""
    raw = _COMMENT_RE.sub(b" ", payload.encode("utf-8", errors="replace"))
    text = raw.decode("utf-8", errors="replace")
    text = re.sub(r">\s+<", "><", text)
    return text.strip()


def preprocess(
    content: str,
    content_type: ContentType,
    *,
    max_chars: int | None = 60_000,
    keep_links: bool = False,
) -> str:
    """Prepare content for extraction according to its type.

    Args:
        content: Raw body text.
        content_type: How to treat it.
        max_chars: Truncate the result to this length. ``None`` disables
            truncation.
        keep_links: Forwarded to :func:`html_to_text`.

    Returns:
        The trimmed content, truncated with an explicit marker when it was too
        long — silent truncation would make a missing field look absent rather
        than cut off.
    """
    if content_type is ContentType.HTML:
        result = html_to_text(content, keep_links=keep_links)
    elif content_type is ContentType.JSON:
        result = trim_json(content)
    elif content_type is ContentType.XML:
        result = trim_xml(content)
    else:
        result = _collapse(content)

    if max_chars is not None and len(result) > max_chars:
        omitted = len(result) - max_chars
        result = result[:max_chars] + f"\n\n[...truncated, {omitted} characters omitted...]"
    return result
