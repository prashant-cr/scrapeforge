"""Deterministic, token-free extraction via CSS or JSON path selectors.

When the page structure is known and stable, an LLM call is pure overhead. This
parser maps schema fields to selectors and fills the same Pydantic model, so
callers can switch between ``parser="llm"`` and ``parser="css"`` without touching
their schema or the rest of their code.

CSS syntax (HTML)::

    {"name": "h1.title",              # element text
     "price": ".price@data-amount",   # element attribute
     "tags": "ul.tags li[]"}          # all matches, as a list

Path syntax (JSON)::

    {"name": "product.title",
     "price": "product.offers.0.price",
     "tags": "product.tags[]"}
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from ..exceptions import ConfigError, ParseError
from ..models import ContentType
from .base import BaseParser, SchemaT

__all__ = ["SelectorParser"]


class SelectorParser(BaseParser):
    """Fill a schema from CSS selectors (HTML) or dotted paths (JSON).

    Args:
        selectors: Field name -> selector. Fields with no selector are left to
            the schema's own defaults.
        mode: ``"css"`` for HTML, ``"jsonpath"`` for JSON, or ``"auto"`` to pick
            from the content type at parse time.
    """

    name = "selector"

    def __init__(self, selectors: dict[str, str], *, mode: str = "auto") -> None:
        if not selectors:
            raise ConfigError("SelectorParser requires a non-empty selector mapping")
        if mode not in ("auto", "css", "jsonpath"):
            raise ConfigError(f"Unknown selector mode {mode!r}; expected auto, css, or jsonpath")
        self.selectors = selectors
        self.mode = mode

    # -- CSS ---------------------------------------------------------------

    @staticmethod
    def _extract_css(html: str, selector: str) -> Any:
        try:
            from selectolax.parser import HTMLParser
        except ImportError as exc:  # pragma: no cover - core dependency
            raise ConfigError("CSS extraction requires selectolax") from exc

        many = selector.endswith("[]")
        if many:
            selector = selector[:-2]

        attribute = None
        if "@" in selector:
            selector, attribute = selector.rsplit("@", 1)

        tree = HTMLParser(html)
        nodes = tree.css(selector.strip())

        def value_of(node: Any) -> str | None:
            if attribute:
                return node.attributes.get(attribute)
            text = node.text(strip=True)
            return text or None

        if many:
            return [v for v in (value_of(n) for n in nodes) if v is not None]
        return value_of(nodes[0]) if nodes else None

    # -- JSON path ---------------------------------------------------------

    @staticmethod
    def _extract_path(data: Any, path: str) -> Any:
        """Resolve a dotted path with numeric indices and a trailing ``[]``."""
        many = path.endswith("[]")
        if many:
            path = path[:-2]

        current = data
        for segment in filter(None, path.split(".")):
            if current is None:
                return None
            if isinstance(current, list):
                if not segment.isdigit():
                    return None
                index = int(segment)
                current = current[index] if -len(current) <= index < len(current) else None
            elif isinstance(current, dict):
                current = current.get(segment)
            else:
                return None

        if many and not isinstance(current, list):
            return [] if current is None else [current]
        return current

    # -- entry point -------------------------------------------------------

    async def parse(
        self,
        content: str,
        schema: type[SchemaT],
        *,
        content_type: ContentType = ContentType.HTML,
        instructions: str | None = None,
    ) -> SchemaT:
        """Fill ``schema`` from ``content`` using the configured selectors.

        ``instructions`` is accepted for interface compatibility and ignored —
        this parser is deterministic and has nothing to steer.

        Raises:
            ParseError: If the content cannot be parsed or the result does not
                validate against ``schema``.
            ConfigError: If the mode and content type are incompatible.
        """
        mode = self.mode
        if mode == "auto":
            mode = "jsonpath" if content_type is ContentType.JSON else "css"

        values: dict[str, Any] = {}

        if mode == "jsonpath":
            import json

            try:
                data = json.loads(content)
            except ValueError as exc:
                raise ParseError(
                    f"Content is not valid JSON: {exc}", schema=schema.__name__
                ) from exc
            for field, path in self.selectors.items():
                values[field] = self._extract_path(data, path)
        else:
            if content_type is ContentType.JSON:
                raise ConfigError("CSS selectors cannot be applied to JSON content")
            for field, selector in self.selectors.items():
                values[field] = self._extract_css(content, selector)

        # Drop unresolved selectors rather than passing None: an explicit None
        # would override the schema's own default, so a missing optional field
        # would fail validation instead of falling back. A genuinely required
        # field still errors, which is the right outcome.
        values = {k: v for k, v in values.items() if v is not None}

        try:
            return schema.model_validate(values)
        except ValidationError as exc:
            raise ParseError(
                "Selector output did not validate against the schema",
                schema=schema.__name__,
                validation_error=exc,
            ) from exc
