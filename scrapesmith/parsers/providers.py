"""Provider routing for structured extraction.

The parser depends on this module, never on a concrete vendor SDK. Every
provider is normalized behind :class:`StructuredClient`, whose single method
takes messages plus a Pydantic model and returns a validated instance.

``instructor`` does the schema plumbing (tool-calling for Anthropic and OpenAI,
JSON mode for Ollama) and the SDK-level retry on malformed output.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypeVar

from pydantic import BaseModel

from ..config import ScraperConfig
from ..exceptions import ConfigError

__all__ = ["StructuredClient", "get_client"]

SchemaT = TypeVar("SchemaT", bound=BaseModel)

#: Base URL used when a local Ollama endpoint is not configured explicitly.
DEFAULT_OLLAMA_BASE_URL = "http://localhost:11434/v1"


def _require(package: str, extra: str = "scrapesmith[llm]") -> Any:
    """Import an optional provider SDK, or raise a ConfigError with a fix."""
    try:
        return __import__(package)
    except ImportError as exc:
        raise ConfigError(
            f"The {package!r} package is required for this LLM provider. Install {extra}."
        ) from exc


@dataclass
class StructuredClient:
    """A provider-agnostic handle for schema-constrained completion.

    Attributes:
        provider: The provider name this client wraps.
        model: Model id used for every call.
        client: The underlying ``instructor``-patched async client.
        default_kwargs: Provider-specific arguments merged into every call.
    """

    provider: str
    model: str
    client: Any
    default_kwargs: dict[str, Any]

    async def create(
        self,
        *,
        system: str,
        user: str,
        response_model: type[SchemaT],
        max_retries: int = 1,
        extra: dict[str, Any] | None = None,
    ) -> SchemaT:
        """Run one schema-constrained completion.

        Args:
            system: System prompt.
            user: User message carrying the content to extract from.
            response_model: The Pydantic model to fill and validate.
            max_retries: Bounded re-asks performed by ``instructor`` when the
                model's output fails validation.
            extra: Additional provider arguments for this call only.

        Returns:
            A validated ``response_model`` instance.
        """
        kwargs: dict[str, Any] = dict(self.default_kwargs)
        if extra:
            kwargs.update(extra)

        if self.provider == "anthropic":
            # Anthropic takes the system prompt as a top-level parameter.
            return await self.client.messages.create(
                model=self.model,
                system=system,
                messages=[{"role": "user", "content": user}],
                response_model=response_model,
                max_retries=max_retries,
                **kwargs,
            )

        return await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_model=response_model,
            max_retries=max_retries,
            **kwargs,
        )

    async def aclose(self) -> None:
        """Close the underlying SDK client if it exposes a close method."""
        inner = getattr(self.client, "client", None)
        for candidate in (inner, self.client):
            close = getattr(candidate, "close", None)
            if close is not None:
                result = close()
                if hasattr(result, "__await__"):
                    await result
                return


def get_client(config: ScraperConfig) -> StructuredClient:
    """Build a :class:`StructuredClient` for the configured provider.

    Args:
        config: Client configuration. ``llm_provider``, ``llm_model``,
            ``llm_api_key``, and ``llm_base_url`` are read here.

    Returns:
        A ready-to-use :class:`StructuredClient`.

    Raises:
        ConfigError: For an unknown provider, a missing SDK, or a missing key.
    """
    instructor = _require("instructor")
    provider = config.llm_provider
    model = config.resolve_model()
    api_key = config.resolve_api_key()
    defaults: dict[str, Any] = dict(config.llm_extra)

    if provider == "anthropic":
        anthropic = _require("anthropic")
        # max_tokens is required by the Anthropic API. On models where thinking is
        # on by default it also covers thinking, so keep the ceiling generous
        # rather than disabling thinking (which degrades tool-call reliability,
        # and structured output is implemented via tool calls).
        defaults.setdefault("max_tokens", config.llm_max_tokens)
        client = instructor.from_anthropic(
            anthropic.AsyncAnthropic(api_key=api_key, base_url=config.llm_base_url or None)
        )
        return StructuredClient(provider, model, client, defaults)

    if provider in ("openai", "openai_compatible", "ollama"):
        openai = _require("openai")
        base_url = config.llm_base_url
        if provider == "ollama" and not base_url:
            base_url = DEFAULT_OLLAMA_BASE_URL
        if provider == "openai_compatible" and not base_url:
            raise ConfigError("llm_base_url is required for the 'openai_compatible' provider")

        raw = openai.AsyncOpenAI(api_key=api_key, base_url=base_url)
        # Local models generally lack reliable tool-calling; JSON mode is the
        # dependable path there.
        mode = instructor.Mode.JSON if provider == "ollama" else instructor.Mode.TOOLS
        client = instructor.from_openai(raw, mode=mode)
        return StructuredClient(provider, model, client, defaults)

    raise ConfigError(
        f"Unknown llm_provider {provider!r}. "
        "Expected one of: openai, anthropic, ollama, openai_compatible."
    )
