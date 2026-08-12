"""Pure parsers for the four well-known discovery files.

Everything here is a pure function over text or bytes — no I/O, no network — so
each format can be tested against a fixture and reasoned about on its own. The
fetching, recursion, and limits live in :mod:`scrapeforge.discovery.discoverer`.
"""

from __future__ import annotations

import gzip
import re
import xml.etree.ElementTree as ET
from urllib.parse import urljoin, urlsplit

from ..exceptions import ParseError
from .models import AiTxtInfo, DiscoveredLink, LinkSource, RobotsInfo

__all__ = [
    "parse_ai_txt",
    "parse_llms_txt",
    "parse_robots_txt",
    "parse_sitemap",
]

#: Sitemaps are frequently served gzipped, and always at `.xml.gz`.
_GZIP_MAGIC = b"\x1f\x8b"

#: A document declaring entities is either malicious or broken; either way we
#: will not hand it to the XML parser. This closes XXE and entity-expansion
#: ("billion laughs") vectors without pulling in a hardened XML dependency,
#: since sitemaps have no legitimate reason to declare a DTD.
_DANGEROUS_XML = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY)", re.IGNORECASE)

_MD_LINK = re.compile(r"\[([^\]]*)\]\(\s*<?([^)\s>]+)>?(?:\s+[\"'][^\"']*[\"'])?\s*\)")
_MD_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_BARE_URL = re.compile(r"https?://[^\s<>\"')\]]+")


def _clean_url(value: str, base: str | None) -> str | None:
    """Normalize one URL, resolving it against ``base`` when relative."""
    value = (value or "").strip()
    if not value or value.startswith(("#", "javascript:", "mailto:", "data:")):
        return None
    if base:
        value = urljoin(base, value)
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return value


# --- robots.txt ---------------------------------------------------------------


def parse_robots_txt(text: str, *, url: str = "", user_agent: str = "*") -> RobotsInfo:
    """Parse ``robots.txt``, keeping the parts a crawler actually wants.

    ``Sitemap:`` directives are collected regardless of which user-agent group
    they appear in — per the specification they are site-wide, not agent-scoped,
    and in practice they are often written after an unrelated group.

    ``Allow``/``Disallow`` are gathered for ``user_agent``, falling back to the
    ``*`` group when the site has no rules naming that agent specifically.

    Args:
        text: Raw file contents.
        url: URL it was fetched from, used to resolve relative sitemap paths.
        user_agent: Product token whose rules to extract.

    Returns:
        A :class:`~scrapeforge.discovery.models.RobotsInfo`.
    """
    info = RobotsInfo(url=url, raw=text)
    wanted = user_agent.lower()

    groups: dict[str, dict[str, list[str]]] = {}
    delays: dict[str, float] = {}
    current: list[str] = []
    # Per RFC 9309, consecutive User-agent lines accumulate into one group; the
    # first rule line closes the agent list, so a later User-agent line starts a
    # new group. Getting this wrong silently drops rules for every agent in a
    # shared block except the last.
    collecting_agents = True

    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field, _, value = line.partition(":")
        field = field.strip().lower()
        value = value.strip()
        if not value:
            continue

        if field == "user-agent":
            agent = value.lower()
            if agent not in {a.lower() for a in info.user_agents}:
                info.user_agents.append(value)
            if not collecting_agents:
                current = []
                collecting_agents = True
            current.append(agent)
            groups.setdefault(agent, {"allow": [], "disallow": []})
        elif field == "sitemap":
            resolved = _clean_url(value, url or None)
            if resolved and resolved not in info.sitemaps:
                info.sitemaps.append(resolved)
        elif field in ("allow", "disallow"):
            collecting_agents = False
            for agent in current or ["*"]:
                groups.setdefault(agent, {"allow": [], "disallow": []})[field].append(value)
        elif field == "crawl-delay":
            collecting_agents = False
            try:
                delay = float(value)
            except ValueError:
                continue
            for agent in current or ["*"]:
                delays[agent] = delay

    chosen = groups.get(wanted) or groups.get("*") or {"allow": [], "disallow": []}
    info.allow = chosen["allow"]
    info.disallow = chosen["disallow"]
    info.crawl_delay = delays.get(wanted, delays.get("*"))
    return info


# --- sitemap.xml --------------------------------------------------------------


def _decompress(body: bytes) -> bytes:
    if body[:2] == _GZIP_MAGIC:
        try:
            return gzip.decompress(body)
        except Exception as exc:
            # Truncated data raises EOFError, a bad header raises BadGzipFile,
            # and a mangled trailer can raise struct.error — none of which share
            # a base class. A remote document must not be able to throw a
            # non-scrapeforge exception at the caller, so catch broadly here.
            raise ParseError(
                f"sitemap looked gzipped but would not decompress: {type(exc).__name__}: {exc}"
            ) from exc
    return body


def _localname(tag: str) -> str:
    """Strip the XML namespace. Sitemaps are namespaced; some real ones are not."""
    return tag.rsplit("}", 1)[-1].lower()


def parse_sitemap(body: bytes, *, url: str = "") -> list[DiscoveredLink]:
    """Parse a sitemap or sitemap index into links.

    Handles both document types defined by the sitemap protocol — a ``urlset``
    of pages and a ``sitemapindex`` of further sitemaps — and transparently
    decompresses the gzipped form that ``.xml.gz`` URLs serve. Entries from an
    index are flagged ``is_sitemap=True`` so the caller can decide whether to
    recurse.

    Args:
        body: Raw response body, gzipped or plain.
        url: URL it came from, used to resolve relative ``<loc>`` values and
            recorded on each link as ``found_in``.

    Returns:
        The links in document order.

    Raises:
        ParseError: If the body is not parseable XML, or declares a DTD/entity
            (rejected outright rather than parsed).
    """
    body = _decompress(body)
    if not body.strip():
        return []

    if _DANGEROUS_XML.search(body[:8192]):
        raise ParseError(
            "sitemap declares a DTD or entity; refusing to parse it "
            "(entity expansion and external-entity attacks)"
        )

    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise ParseError(f"sitemap is not valid XML: {exc}") from exc

    root_name = _localname(root.tag)
    if root_name not in ("urlset", "sitemapindex"):
        raise ParseError(
            f"unexpected sitemap root element {root_name!r}; expected urlset or sitemapindex"
        )

    is_index = root_name == "sitemapindex"
    links: list[DiscoveredLink] = []
    seen: set[str] = set()

    for entry in root:
        if _localname(entry.tag) not in ("url", "sitemap"):
            continue
        fields: dict[str, str] = {}
        for child in entry:
            name = _localname(child.tag)
            if child.text:
                fields[name] = child.text.strip()

        location = _clean_url(fields.get("loc", ""), url or None)
        if not location or location in seen:
            continue
        seen.add(location)

        priority: float | None = None
        if "priority" in fields:
            try:
                priority = float(fields["priority"])
            except ValueError:
                priority = None

        links.append(
            DiscoveredLink(
                url=location,
                source=LinkSource.SITEMAP,
                lastmod=fields.get("lastmod"),
                changefreq=fields.get("changefreq"),
                priority=priority,
                is_sitemap=is_index,
                found_in=url or None,
            )
        )
    return links


# --- llms.txt -----------------------------------------------------------------


def parse_llms_txt(text: str, *, url: str = "") -> list[DiscoveredLink]:
    """Parse an ``llms.txt`` file into links.

    The format (llmstxt.org) is Markdown: an ``#`` title, an optional ``>``
    summary, then ``##`` sections of list items shaped
    ``- [Title](url): description``. Section headings are carried onto each
    link, because "which section was this under" is usually the most useful
    signal in the file — an ``## Optional`` section means something different
    from ``## Docs``.

    Bare URLs outside list items are picked up too, since real files in the wild
    are looser than the specification.

    Args:
        text: Raw file contents.
        url: URL it came from, for resolving relative links.

    Returns:
        Links in document order, de-duplicated by URL.
    """
    links: list[DiscoveredLink] = []
    seen: set[str] = set()
    section: str | None = None
    in_fence = False

    for raw_line in text.splitlines():
        line = raw_line.rstrip()

        # Never read links out of a fenced code block.
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence:
            continue

        heading = _MD_HEADING.match(line.strip())
        if heading:
            level, title = heading.group(1), heading.group(2).strip()
            if len(level) >= 2:
                section = title
            continue

        matched_any = False
        for match in _MD_LINK.finditer(line):
            title, target = match.group(1).strip(), match.group(2)
            resolved = _clean_url(target, url or None)
            if not resolved or resolved in seen:
                matched_any = True
                continue
            seen.add(resolved)
            matched_any = True

            # "- [Title](url): description" — the description follows the link.
            tail = line[match.end() :].lstrip()
            description = tail[1:].strip() if tail.startswith(":") else (tail or None)

            links.append(
                DiscoveredLink(
                    url=resolved,
                    source=LinkSource.LLMS,
                    title=title or None,
                    description=description or None,
                    section=section,
                    found_in=url or None,
                )
            )

        if not matched_any:
            for match in _BARE_URL.finditer(line):
                resolved = _clean_url(match.group(0).rstrip(".,;"), url or None)
                if not resolved or resolved in seen:
                    continue
                seen.add(resolved)
                links.append(
                    DiscoveredLink(
                        url=resolved,
                        source=LinkSource.LLMS,
                        section=section,
                        found_in=url or None,
                    )
                )
    return links


# --- ai.txt -------------------------------------------------------------------


def parse_ai_txt(text: str, *, url: str = "", user_agent: str = "*") -> AiTxtInfo:
    """Parse an ``ai.txt`` AI-training permissions file.

    The convention borrows robots.txt's shape (``User-Agent``, ``Allow``,
    ``Disallow``) but there is no single ratified spec, so every other
    ``Key: value`` directive is kept verbatim in ``directives`` rather than
    discarded — a file saying something we do not model is still worth showing
    the caller.

    Args:
        text: Raw file contents.
        url: URL it came from.
        user_agent: Product token whose rules to extract.

    Returns:
        An :class:`~scrapeforge.discovery.models.AiTxtInfo`.
    """
    info = AiTxtInfo(url=url, raw=text)
    wanted = user_agent.lower()
    groups: dict[str, dict[str, list[str]]] = {}
    current: list[str] = []

    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field, _, value = line.partition(":")
        field = field.strip().lower()
        value = value.strip()
        if not value:
            continue

        if field in ("user-agent", "useragent"):
            agent = value.lower()
            if value not in info.user_agents:
                info.user_agents.append(value)
            current = [agent]
            groups.setdefault(agent, {"allow": [], "disallow": []})
        elif field in ("allow", "disallow"):
            for agent in current or ["*"]:
                groups.setdefault(agent, {"allow": [], "disallow": []})[field].append(value)
        else:
            info.directives.setdefault(field, []).append(value)

    chosen = groups.get(wanted) or groups.get("*") or {"allow": [], "disallow": []}
    info.allow = chosen["allow"]
    info.disallow = chosen["disallow"]
    return info
