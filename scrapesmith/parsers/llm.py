"""LLM-backed extraction: content + schema -> validated instance."""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ValidationError

from ..config import ScraperConfig
from ..exceptions import ConfigError, ParseError
from ..models import ContentType
from .base import BaseParser, SchemaT
from .preprocess import preprocess
from .providers import StructuredClient, get_client

__all__ = ["EXTRACTION_PROMPT", "LLMParser"]

logger = logging.getLogger(__name__)

#: Deliberately small and generic. Field-level guidance belongs in the user's
#: schema (``Field(description=...)``), which is forwarded to the model as part
#: of the JSON schema — that keeps the contract in one place.
EXTRACTION_PROMPT = """\
You extract structured data from web content.

Extract the fields defined by the provided schema from the content below.
Use null for anything genuinely absent from the content; do not invent values,
and do not infer a value from general knowledge. Copy values as they appear,
converting only to match the field's type (for example, strip a currency symbol
from a numeric price field and report the currency separately if the schema asks
for it)."""


class LLMParser(BaseParser):
    """Fills a Pydantic schema from content using a provider-agnostic LLM call.

    The user's model *is* the contract: it is sent as a JSON schema, the response
    is constrained to it, and validation happens before the value is returned.
    On a validation failure the underlying client performs a bounded re-ask with
    the error fed back; if that still fails, :class:`~scrapesmith.exceptions.ParseError`
    is raised rather than a half-filled object.

    Args:
        config: Client configuration providing the provider and model.
        client: Pre-built client, mainly for tests. Built lazily from ``config``
            when omitted, so importing scrapesmith never requires an LLM SDK.
    """

    name = "llm"

    def __init__(self, config: ScraperConfig, *, client: StructuredClient | None = None) -> None:
        self.config = config
        self._client = client

    @property
    def client(self) -> StructuredClient:
        """The structured client, constructed on first use."""
        if self._client is None:
            self._client = get_client(self.config)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _build_user_message(
        self, content: str, schema: type[BaseModel], instructions: str | None
    ) -> str:
        parts = []
        if instructions:
            parts.append(f"Additional instructions:\n{instructions}\n")
        parts.append(f"Target record: {schema.__name__}")
        doc = (schema.__doc__ or "").strip()
        if doc and not doc.startswith(f"{schema.__name__}("):
            parts.append(f"Description: {doc}")
        parts.append("\nContent:\n---\n" + content + "\n---")
        return "\n".join(parts)

    async def parse(
        self,
        content: str,
        schema: type[SchemaT],
        *,
        content_type: ContentType = ContentType.HTML,
        instructions: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> SchemaT:
        """Extract ``schema`` from ``content``.

        Args:
            content: Raw HTML, JSON, or XML.
            schema: The Pydantic model to fill.
            content_type: Drives preprocessing.
            instructions: Extra guidance prepended to the user message.
            extra: Additional provider arguments for this call only.

        Returns:
            A validated instance of ``schema``.

        Raises:
            ParseError: If the content is empty, the provider call fails, or the
                result does not validate after the bounded re-ask.
            ConfigError: If the LLM provider is misconfigured.
        """
        cleaned = preprocess(content, content_type, max_chars=self.config.max_content_chars)
        if not cleaned.strip():
            raise ParseError(
                "Nothing to extract: content is empty after preprocessing", schema=schema.__name__
            )

        logger.debug(
            "Extracting %s from %s chars of %s via %s/%s",
            schema.__name__,
            len(cleaned),
            content_type.value,
            self.config.llm_provider,
            self.config.resolve_model(),
        )

        try:
            client = self.client
        except ConfigError:
            raise
        except Exception as exc:  # pragma: no cover - SDK construction failure
            raise ParseError(
                f"Could not build the LLM client: {type(exc).__name__}: {exc}",
                schema=schema.__name__,
            ) from exc

        try:
            return await client.create(
                system=EXTRACTION_PROMPT,
                user=self._build_user_message(cleaned, schema, instructions),
                response_model=schema,
                # One bounded re-ask with the validation error fed back; beyond
                # that, failing loudly beats returning a plausible-looking guess.
                max_retries=1,
                extra=extra,
            )
        except ValidationError as exc:
            raise ParseError(
                "LLM output did not validate against the schema after one retry",
                schema=schema.__name__,
                validation_error=exc,
            ) from exc
        except ConfigError:
            raise
        except Exception as exc:
            raise ParseError(
                f"LLM extraction failed: {type(exc).__name__}: {exc}",
                schema=schema.__name__,
                validation_error=exc if isinstance(exc, Exception) else None,
            ) from exc
