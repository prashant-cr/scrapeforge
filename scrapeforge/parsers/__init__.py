"""Extraction: preprocessing, provider routing, LLM and selector parsers."""

from __future__ import annotations

from .base import BaseParser
from .llm import LLMParser
from .preprocess import html_to_text, preprocess, trim_json, trim_xml
from .providers import StructuredClient, get_client
from .selector import SelectorParser

__all__ = [
    "BaseParser",
    "LLMParser",
    "SelectorParser",
    "StructuredClient",
    "get_client",
    "html_to_text",
    "preprocess",
    "trim_json",
    "trim_xml",
]
