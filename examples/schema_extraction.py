"""Extract a Pydantic schema from a page — the core use case.

Runs offline against a bundled fixture so it works without network access. Set
ANTHROPIC_API_KEY (or pass llm_api_key) to run the LLM path; without a key the
example falls back to the deterministic CSS path, which needs no API at all.

    pip install "scrapekit[llm]"
    export ANTHROPIC_API_KEY=...
    python examples/schema_extraction.py
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from pydantic import BaseModel, Field

from scrapekit import ContentType, ParseError, Scraper

FIXTURE = Path(__file__).parent.parent / "tests" / "fixtures" / "product.html"


class Product(BaseModel):
    """A product listing from a shop page."""

    # Field descriptions are forwarded to the model as part of the JSON schema,
    # so extraction guidance belongs here rather than in a prompt.
    name: str = Field(description="The product's display name")
    price: float = Field(description="Numeric price only, without a currency symbol")
    currency: str = Field(default="USD", description="ISO 4217 code, e.g. USD")
    in_stock: bool = Field(default=False, description="Whether it can be ordered now")
    tags: list[str] = Field(default_factory=list, description="Category or feature tags")


async def extract_with_llm(scraper: Scraper, html: str) -> Product:
    """The default path: the model reads the page and fills the schema."""
    return await scraper.extract(html, Product, content_type=ContentType.HTML)


async def extract_with_selectors(scraper: Scraper, html: str) -> Product:
    """The token-free path, for pages whose structure you already know."""
    return await scraper.extract(
        html,
        Product,
        content_type=ContentType.HTML,
        parser="css",
        selectors={
            "name": "h1.title",
            "price": ".price@data-amount",  # attribute instead of text
            "currency": ".price@data-currency",
            "in_stock": ".availability@data-in-stock",
            "tags": "ul.tags li[]",  # trailing [] collects every match
        },
    )


async def main() -> None:
    html = FIXTURE.read_text()
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))

    async with Scraper(
        llm_provider="anthropic",
        llm_model="claude-opus-5",
        respect_robots=True,
    ) as scraper:
        # Same schema, same call shape — only the parser differs.
        product = await extract_with_selectors(scraper, html)
        print(f"[css]  {product!r}")

        if not has_key:
            print("\n[llm]  skipped — set ANTHROPIC_API_KEY to run the LLM path")
            return

        try:
            product = await extract_with_llm(scraper, html)
        except ParseError as exc:
            # A half-filled object is never returned; failures are loud.
            print(f"\n[llm]  extraction failed: {exc}")
            return
        print(f"[llm]  {product!r}")

    # In real use you would scrape a live URL instead of a fixture:
    #
    #     product = await scraper.scrape("https://shop.example/p/1", schema=Product)


if __name__ == "__main__":
    asyncio.run(main())
