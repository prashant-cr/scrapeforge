"""Fetch strategies and the fallback chain that orchestrates them."""

from __future__ import annotations

from .base import BaseFetcher
from .browser import PlaywrightFetcher
from .chain import FETCHER_REGISTRY, FallbackChain, register_fetcher
from .http import HttpxFetcher
from .impersonate import CurlCffiFetcher
from .tls import TlsClientFetcher

__all__ = [
    "FETCHER_REGISTRY",
    "BaseFetcher",
    "CurlCffiFetcher",
    "FallbackChain",
    "HttpxFetcher",
    "PlaywrightFetcher",
    "TlsClientFetcher",
    "register_fetcher",
]
