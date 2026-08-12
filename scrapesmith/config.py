"""Pydantic configuration models.

Two objects define everything scrapesmith does:

* :class:`ScraperConfig` — client-wide defaults, passed once to ``Scraper(...)``.
* :class:`FetchOptions` — per-request overrides. Merge order is
  ``FetchOptions`` over ``ScraperConfig``; user-supplied headers and cookies are
  merged (per-call keys win) rather than replaced.
"""

from __future__ import annotations

import os
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from .exceptions import ConfigError
from .models import ContentType

__all__ = ["KNOWN_STRATEGIES", "FetchOptions", "LLMProvider", "ScraperConfig"]

LLMProvider = Literal["openai", "anthropic", "ollama", "openai_compatible"]

#: Strategy names the built-in registry knows about. Custom fetchers registered
#: via :func:`scrapesmith.fetchers.chain.register_fetcher` extend this at runtime.
KNOWN_STRATEGIES: tuple[str, ...] = ("http", "impersonate", "tls", "browser")

#: Environment variables consulted for each provider's API key.
API_KEY_ENV_VARS: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openai_compatible": "OPENAI_API_KEY",
    "ollama": "OLLAMA_API_KEY",
}

#: Default model per provider. Only providers with a stable, well-known default
#: are listed; everything else requires an explicit ``llm_model``.
DEFAULT_MODELS: dict[str, str] = {
    "anthropic": "claude-opus-5",
}


class ScraperConfig(BaseModel):
    """Client-wide defaults for fetching, evasion, and extraction.

    Every field is overridable per request via :class:`FetchOptions`, except the
    LLM settings and the responsible-use limits, which are client-scoped.
    """

    model_config = ConfigDict(extra="forbid")

    # --- LLM ---------------------------------------------------------------
    llm_provider: LLMProvider = "anthropic"
    llm_model: str | None = Field(
        default=None,
        description="Model id. Defaults to the provider's default where one exists.",
    )
    llm_api_key: SecretStr | None = Field(
        default=None,
        description="Explicit API key. Falls back to the provider's environment variable.",
    )
    llm_base_url: str | None = Field(
        default=None,
        description="Base URL for 'ollama' and 'openai_compatible' providers.",
    )
    llm_max_tokens: int = Field(
        default=8192,
        gt=0,
        description=(
            "Output token ceiling for the extraction call. On models with thinking "
            "enabled this covers thinking plus the response, so keep it generous."
        ),
    )
    llm_extra: dict[str, Any] = Field(
        default_factory=dict,
        description="Extra keyword arguments forwarded verbatim to the provider call.",
    )
    max_content_chars: int = Field(
        default=60_000,
        gt=0,
        description="Preprocessed content is truncated to this many characters before extraction.",
    )

    # --- network -----------------------------------------------------------
    proxy: str | None = Field(
        default=None, description="Proxy URL, e.g. http://user:pass@host:port"
    )
    headers: dict[str, str] = Field(default_factory=dict)
    cookies: dict[str, str] = Field(default_factory=dict)
    timeout: float = Field(default=30.0, gt=0)
    max_retries: int = Field(
        default=2,
        ge=0,
        description="Retries per strategy for transient network errors. Blocks escalate instead.",
    )
    verify_ssl: bool = True
    follow_redirects: bool = True

    # --- strategy control --------------------------------------------------
    strategies: list[str] = Field(
        default_factory=lambda: ["http", "impersonate", "browser"],
        description="Fetchers to try, in escalation order.",
    )
    impersonate_target: str = Field(
        default="chrome124",
        description="curl_cffi impersonation target (TLS/JA3 + HTTP2 fingerprint).",
    )
    tls_client_identifier: str = Field(
        default="chrome_120",
        description="tls-client profile used by the 'tls' strategy.",
    )
    browser_type: Literal["chromium", "firefox", "webkit"] = "chromium"
    headless: bool = True
    rotate_user_agent: bool = True

    # --- responsible use ---------------------------------------------------
    respect_robots: bool = Field(
        default=True,
        description="Check robots.txt before fetching. Disabling this is an explicit choice.",
    )
    max_concurrency_per_domain: int = Field(default=4, gt=0)
    min_delay: float = Field(
        default=0.5, ge=0, description="Minimum delay between requests to a host."
    )
    max_delay: float = Field(default=1.5, ge=0, description="Upper bound for the randomized delay.")

    @field_validator("strategies")
    @classmethod
    def _non_empty_strategies(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("strategies must not be empty")
        return value

    @field_validator("max_delay")
    @classmethod
    def _delay_range(cls, value: float, info: Any) -> float:
        min_delay = info.data.get("min_delay", 0.0)
        if value < min_delay:
            raise ValueError("max_delay must be >= min_delay")
        return value

    def resolve_model(self) -> str:
        """Return the model id to use, applying the provider default.

        Raises:
            ConfigError: If no model is configured and the provider has no default.
        """
        if self.llm_model:
            return self.llm_model
        default = DEFAULT_MODELS.get(self.llm_provider)
        if default:
            return default
        raise ConfigError(
            f"llm_model is required for provider {self.llm_provider!r} "
            "(no default is assumed for non-Anthropic providers)"
        )

    def resolve_api_key(self) -> str | None:
        """Return the API key, falling back to the provider's environment variable.

        Returns ``None`` for local providers (Ollama) where a key is optional.

        Raises:
            ConfigError: If a key is required for the provider but not found.
        """
        if self.llm_api_key is not None:
            return self.llm_api_key.get_secret_value()
        env_var = API_KEY_ENV_VARS.get(self.llm_provider)
        key = os.environ.get(env_var) if env_var else None
        if key:
            return key
        if self.llm_provider == "ollama":
            return "ollama"  # local endpoints ignore the value but SDKs require one
        raise ConfigError(
            f"No API key for provider {self.llm_provider!r}. "
            f"Set llm_api_key or the {env_var} environment variable."
        )


class FetchOptions(BaseModel):
    """Per-request overrides.

    ``None`` means "inherit from :class:`ScraperConfig`". Call :meth:`resolve` to
    produce a fully-populated instance for a fetcher to consume.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    method: str = "GET"
    params: dict[str, Any] | None = None
    data: Any = Field(default=None, description="Form body (dict) or raw body (str/bytes).")
    json_body: Any = Field(default=None, alias="json", description="JSON body.")

    headers: dict[str, str] | None = None
    cookies: dict[str, str] | None = None
    proxy: str | None = None
    timeout: float | None = None
    verify_ssl: bool | None = None
    follow_redirects: bool | None = None
    strategies: list[str] | None = None

    # Browser-only knobs; ignored by the HTTP fetchers.
    wait_until: Literal["load", "domcontentloaded", "networkidle", "commit"] | None = None
    wait_for_selector: str | None = None
    wait_time: float | None = Field(
        default=None, ge=0, description="Extra seconds to idle after load, for lazy content."
    )

    respect_robots: bool | None = None
    expected_content_type: ContentType | None = Field(
        default=None,
        description=(
            "Escalate when the response body is not of this type "
            "(e.g. a JSON API that served an HTML block page)."
        ),
    )

    @field_validator("method")
    @classmethod
    def _upper_method(cls, value: str) -> str:
        return value.upper()

    def resolve(self, config: ScraperConfig) -> FetchOptions:
        """Return a copy with every inheritable field filled in from ``config``.

        Headers and cookies are merged (per-request keys win) rather than
        replaced, so a caller adding one header does not lose the client defaults.
        """
        return self.model_copy(
            update={
                "headers": {**config.headers, **(self.headers or {})},
                "cookies": {**config.cookies, **(self.cookies or {})},
                "proxy": self.proxy if self.proxy is not None else config.proxy,
                "timeout": self.timeout if self.timeout is not None else config.timeout,
                "verify_ssl": self.verify_ssl if self.verify_ssl is not None else config.verify_ssl,
                "follow_redirects": (
                    self.follow_redirects
                    if self.follow_redirects is not None
                    else config.follow_redirects
                ),
                "strategies": list(self.strategies or config.strategies),
                "respect_robots": (
                    self.respect_robots
                    if self.respect_robots is not None
                    else config.respect_robots
                ),
                "wait_until": self.wait_until or "domcontentloaded",
            }
        )

    def has_body(self) -> bool:
        """True when the request carries a payload."""
        return self.data is not None or self.json_body is not None
