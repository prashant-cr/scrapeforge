"""Realistic header-set construction.

Bot managers score header *coherence*, not just presence: a Chrome ``User-Agent``
alongside Firefox-only headers, or a ``Sec-CH-UA`` brand list that disagrees with
the UA version, is a stronger signal than a missing header. So headers are always
derived from a :class:`~scrapeforge.fingerprint.user_agents.UserAgentProfile`,
never assembled ad hoc.

Header *order* also differs between browsers and is preserved here — dicts keep
insertion order, and both httpx and curl_cffi respect it.
"""

from __future__ import annotations

import importlib.util

from ..models import ContentType
from .user_agents import UserAgentProfile

__all__ = ["ACCEPT_BY_TYPE", "SUPPORTED_ENCODINGS", "accept_encoding_for", "build_headers"]


def _decoder_available(*modules: str) -> bool:
    return any(importlib.util.find_spec(m) is not None for m in modules)


def _supported_encodings() -> tuple[str, ...]:
    """Content encodings we can actually decode.

    Never advertise an encoding we cannot decode: the server will happily use it
    and the caller gets raw compressed bytes back. The decoders ship as core
    dependencies, so in a normal install this is the full browser-like list; the
    probe exists so a stripped install degrades to gzip/deflate instead of
    returning garbage.
    """
    encodings = ["gzip", "deflate"]
    if _decoder_available("brotli", "brotlicffi"):
        encodings.append("br")
    if _decoder_available("zstandard"):
        encodings.append("zstd")
    return tuple(encodings)


#: Encodings this installation can decode, in the order browsers list them.
SUPPORTED_ENCODINGS: tuple[str, ...] = _supported_encodings()

_ACCEPT_HTML_CHROME = (
    "text/html,application/xhtml+xml,application/xml;q=0.9,"
    "image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"
)
_ACCEPT_HTML_FIREFOX = (
    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
)
_ACCEPT_HTML_SAFARI = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"

#: ``Accept`` values keyed by what the caller expects back.
ACCEPT_BY_TYPE: dict[ContentType, str] = {
    ContentType.JSON: "application/json, text/plain, */*",
    ContentType.XML: "application/xml, text/xml, */*;q=0.9",
}


def accept_encoding_for(profile: UserAgentProfile) -> str:
    """Build ``Accept-Encoding`` for ``profile``, limited to decodable encodings.

    Chrome advertises zstd; Firefox and Safari do not, so listing it for them
    would itself be an incoherence.
    """
    encodings = [e for e in SUPPORTED_ENCODINGS if e != "zstd" or profile.browser == "chrome"]
    return ", ".join(encodings)


def _accept_for(profile: UserAgentProfile, expected: ContentType | None) -> str:
    if expected is not None and expected in ACCEPT_BY_TYPE:
        return ACCEPT_BY_TYPE[expected]
    if profile.browser == "firefox":
        return _ACCEPT_HTML_FIREFOX
    if profile.browser == "safari":
        return _ACCEPT_HTML_SAFARI
    return _ACCEPT_HTML_CHROME


def build_headers(
    profile: UserAgentProfile,
    *,
    expected_content_type: ContentType | None = None,
    referer: str | None = None,
    is_navigation: bool = True,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build a header set coherent with ``profile``.

    Args:
        profile: The user-agent profile driving every derived header.
        expected_content_type: Tunes ``Accept`` (e.g. JSON for an API call).
        referer: Optional ``Referer``; also flips ``Sec-Fetch-Site`` to
            ``same-origin``/``cross-site`` semantics via ``same-site``.
        is_navigation: ``True`` for top-level document requests, ``False`` for
            XHR-style requests (changes ``Sec-Fetch-Mode``/``Dest``).
        extra: Caller-supplied headers. These are applied last — user intent
            always wins over generated values.

    Returns:
        An ordered header dict.
    """
    headers: dict[str, str] = {}

    if profile.is_chromium:
        sec_ch_ua = profile.sec_ch_ua
        if sec_ch_ua:
            headers["sec-ch-ua"] = sec_ch_ua
            headers["sec-ch-ua-mobile"] = "?1" if profile.mobile else "?0"
            headers["sec-ch-ua-platform"] = f'"{profile.platform}"'

    if is_navigation:
        headers["upgrade-insecure-requests"] = "1"

    headers["user-agent"] = profile.user_agent
    headers["accept"] = _accept_for(profile, expected_content_type)

    if profile.browser == "firefox" and expected_content_type is None:
        headers["accept-language"] = profile.accept_language.replace(";q=0.9", ";q=0.5")
    # Sec-Fetch-* are sent by all three modern engines.
    if is_navigation:
        headers["sec-fetch-site"] = "same-origin" if referer else "none"
        headers["sec-fetch-mode"] = "navigate"
        headers["sec-fetch-user"] = "?1"
        headers["sec-fetch-dest"] = "document"
    else:
        headers["sec-fetch-site"] = "same-origin" if referer else "cross-site"
        headers["sec-fetch-mode"] = "cors"
        headers["sec-fetch-dest"] = "empty"

    if referer:
        headers["referer"] = referer

    headers["accept-encoding"] = accept_encoding_for(profile)
    headers.setdefault("accept-language", profile.accept_language)

    if profile.browser == "chrome":
        headers["priority"] = "u=0, i" if is_navigation else "u=1, i"

    if extra:
        # User-supplied headers override generated ones, case-insensitively.
        lowered = {k.lower(): k for k in headers}
        for key, value in extra.items():
            existing = lowered.get(key.lower())
            if existing is not None:
                headers[existing] = value
            else:
                headers[key] = value

    return headers
