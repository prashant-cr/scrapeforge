"""The parser interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TypeVar

from pydantic import BaseModel

from ..models import ContentType

__all__ = ["BaseParser", "SchemaT"]

SchemaT = TypeVar("SchemaT", bound=BaseModel)


class BaseParser(ABC):
    """Turns raw content into a validated instance of the caller's schema."""

    #: Registry key used by ``Scraper(parser=...)``.
    name: str = "base"

    @abstractmethod
    async def parse(
        self,
        content: str,
        schema: type[SchemaT],
        *,
        content_type: ContentType = ContentType.HTML,
        instructions: str | None = None,
    ) -> SchemaT:
        """Extract ``schema`` from ``content``.

        Args:
            content: Raw HTML, JSON, or XML.
            schema: The Pydantic model to fill.
            content_type: How to preprocess ``content``.
            instructions: Optional extra guidance for the extractor.

        Returns:
            A validated instance of ``schema``.

        Raises:
            ParseError: If extraction or validation fails.
        """

    async def aclose(self) -> None:  # noqa: B027 - optional hook, not abstract
        """Release any long-lived resources."""
