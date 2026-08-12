"""The public entrypoint: :class:`Scraper`.

Async-first. The sync mirrors (``fetch_sync``, ``scrape_sync``, ``extract_sync``)
delegate to the async path through an event loop — there is no duplicated logic.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, TypeVar

from pydantic import BaseModel

from .config import FetchOptions, ScraperConfig
from .exceptions import ConfigError, ScrapeforgeError
from .fetchers.chain import FallbackChain
from .models import ContentType, FetchResponse
from .parsers.base import BaseParser
from .parsers.llm import LLMParser
from .parsers.selector import SelectorParser
from .utils.ratelimit import DomainRateLimiter
from .utils.robots import RobotsCache

__all__ = ["Scraper"]

logger = logging.getLogger(__name__)

SchemaT = TypeVar("SchemaT", bound=BaseModel)

#: FetchOptions field names accepted as keyword arguments on fetch/scrape.
_OPTION_FIELDS = frozenset(FetchOptions.model_fields) | {"json"}


class Scraper:
    """Fetches URLs through a fallback chain and extracts them into your schema.

    Example::

        scraper = Scraper(llm_provider="anthropic", llm_model="claude-opus-5")
        product = await scraper.scrape("https://shop.example/p/1", schema=Product)
        await scraper.aclose()

    Args:
        config: A prebuilt :class:`~scrapeforge.config.ScraperConfig`. Mutually
            exclusive with ``**overrides``.
        **overrides: Individual ``ScraperConfig`` fields, for the common case
            where you do not want to build the config object yourself.

    Raises:
        ConfigError: If both ``config`` and ``overrides`` are given, or a field
            is invalid.
    """

    def __init__(self, config: ScraperConfig | None = None, **overrides: Any) -> None:
        if config is not None and overrides:
            raise ConfigError("Pass either a ScraperConfig or keyword overrides, not both")
        try:
            self.config = config or ScraperConfig(**overrides)
        except ScrapeforgeError:
            raise
        except Exception as exc:  # pydantic ValidationError
            raise ConfigError(f"Invalid configuration: {exc}") from exc

        self.chain = FallbackChain(self.config)
        self.robots = RobotsCache()
        self.limiter = DomainRateLimiter(
            max_concurrency=self.config.max_concurrency_per_domain,
            min_delay=self.config.min_delay,
            max_delay=self.config.max_delay,
        )
        self._llm_parser: LLMParser | None = None

    # -- lifecycle ---------------------------------------------------------

    async def aclose(self) -> None:
        """Release the browser, HTTP clients, and LLM client."""
        await self.chain.aclose()
        if self._llm_parser is not None:
            await self._llm_parser.aclose()
            self._llm_parser = None

    async def __aenter__(self) -> Scraper:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    def close_sync(self) -> None:
        """Synchronous mirror of :meth:`aclose`."""
        self._run(self.aclose())

    def __enter__(self) -> Scraper:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close_sync()

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _run(coro: Any) -> Any:
        """Run a coroutine from sync code, refusing to nest inside a live loop."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)
        coro.close()
        raise ConfigError(
            "The *_sync methods cannot be called from inside a running event loop. "
            "Await the async method instead."
        )

    def _build_options(self, kwargs: dict[str, Any]) -> FetchOptions:
        unknown = set(kwargs) - _OPTION_FIELDS
        if unknown:
            raise ConfigError(
                f"Unknown fetch option(s): {', '.join(sorted(unknown))}. "
                f"Valid options: {', '.join(sorted(_OPTION_FIELDS))}"
            )
        try:
            options = FetchOptions(**kwargs)
        except ScrapeforgeError:
            raise
        except Exception as exc:
            raise ConfigError(f"Invalid fetch options: {exc}") from exc
        return options.resolve(self.config)

    @property
    def llm_parser(self) -> LLMParser:
        """The shared LLM parser, constructed on first use."""
        if self._llm_parser is None:
            self._llm_parser = LLMParser(self.config)
        return self._llm_parser

    def _resolve_parser(
        self, parser: str | BaseParser, selectors: dict[str, str] | None
    ) -> BaseParser:
        if isinstance(parser, BaseParser):
            return parser
        if parser == "llm":
            return self.llm_parser
        if parser in ("css", "jsonpath", "selector"):
            if not selectors:
                raise ConfigError(f"parser={parser!r} requires a 'selectors' mapping")
            mode = "auto" if parser == "selector" else parser
            return SelectorParser(selectors, mode=mode)
        raise ConfigError(
            f"Unknown parser {parser!r}; expected 'llm', 'css', 'jsonpath', "
            "or a BaseParser instance"
        )

    # -- public API --------------------------------------------------------

    async def fetch(self, url: str, **options: Any) -> FetchResponse:
        """Fetch a URL through the fallback chain, without parsing.

        Args:
            url: Absolute URL.
            **options: Any :class:`~scrapeforge.config.FetchOptions` field —
                ``method``, ``params``, ``data``, ``json``, ``headers``,
                ``cookies``, ``proxy``, ``timeout``, ``strategies``, and the
                browser waiting knobs.

        Returns:
            A :class:`~scrapeforge.models.FetchResponse`. ``strategy_used`` records
            which fetcher produced it.

        Raises:
            AllStrategiesFailed: Every strategy failed.
            ChallengeError: Every strategy was blocked by bot management.
            ConfigError: Invalid options or an unknown strategy.
        """
        resolved = self._build_options(options)

        if resolved.respect_robots:
            allowed = await self.robots.allowed(
                url, proxy=resolved.proxy, verify_ssl=bool(resolved.verify_ssl)
            )
            if not allowed:
                raise ConfigError(
                    f"robots.txt disallows fetching {url}. "
                    "Pass respect_robots=False to override (your responsibility)."
                )

        async with self.limiter.slot(url):
            return await self.chain.fetch(url, resolved)

    async def scrape(
        self,
        url: str,
        schema: type[SchemaT],
        *,
        parser: str | BaseParser = "llm",
        selectors: dict[str, str] | None = None,
        instructions: str | None = None,
        **options: Any,
    ) -> SchemaT:
        """Fetch a URL and extract it into ``schema``.

        Args:
            url: Absolute URL.
            schema: The Pydantic model to fill.
            parser: ``"llm"`` (default), ``"css"``, ``"jsonpath"``, or a custom
                :class:`~scrapeforge.parsers.base.BaseParser`.
            selectors: Field-to-selector mapping, required for the non-LLM parsers.
            instructions: Extra guidance passed to the LLM parser.
            **options: Forwarded to :meth:`fetch`.

        Returns:
            A validated instance of ``schema``.

        Raises:
            FetchError: The page could not be retrieved.
            ParseError: Extraction or validation failed.
        """
        response = await self.fetch(url, **options)
        return await self.extract(
            response,
            schema,
            parser=parser,
            selectors=selectors,
            instructions=instructions,
        )

    async def extract(
        self,
        content: str | FetchResponse,
        schema: type[SchemaT],
        *,
        content_type: ContentType | str | None = None,
        parser: str | BaseParser = "llm",
        selectors: dict[str, str] | None = None,
        instructions: str | None = None,
    ) -> SchemaT:
        """Extract ``schema`` from content you already have. No network access.

        Args:
            content: Raw text, or a :class:`~scrapeforge.models.FetchResponse`
                (whose detected content type is used automatically).
            schema: The Pydantic model to fill.
            content_type: Override the content type. Required for raw strings
                that are not HTML.
            parser: ``"llm"``, ``"css"``, ``"jsonpath"``, or a parser instance.
            selectors: Field-to-selector mapping for the non-LLM parsers.
            instructions: Extra guidance passed to the LLM parser.

        Returns:
            A validated instance of ``schema``.

        Raises:
            ParseError: Extraction or validation failed.
        """
        if isinstance(content, FetchResponse):
            text = content.text
            resolved_type = content.content_type
        else:
            text = content
            resolved_type = ContentType.HTML

        if content_type is not None:
            resolved_type = (
                content_type if isinstance(content_type, ContentType) else ContentType(content_type)
            )

        selected = self._resolve_parser(parser, selectors)
        if isinstance(selected, LLMParser):
            return await selected.parse(
                text, schema, content_type=resolved_type, instructions=instructions
            )
        return await selected.parse(text, schema, content_type=resolved_type)

    # -- sync mirrors ------------------------------------------------------

    def fetch_sync(self, url: str, **options: Any) -> FetchResponse:
        """Synchronous mirror of :meth:`fetch`."""
        return self._run(self.fetch(url, **options))

    def scrape_sync(self, url: str, schema: type[SchemaT], **kwargs: Any) -> SchemaT:
        """Synchronous mirror of :meth:`scrape`."""
        return self._run(self.scrape(url, schema, **kwargs))

    def extract_sync(
        self, content: str | FetchResponse, schema: type[SchemaT], **kwargs: Any
    ) -> SchemaT:
        """Synchronous mirror of :meth:`extract`."""
        return self._run(self.extract(content, schema, **kwargs))

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<Scraper strategies={self.config.strategies} "
            f"provider={self.config.llm_provider!r} robots={self.config.respect_robots}>"
        )
