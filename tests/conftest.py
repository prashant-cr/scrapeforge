"""Shared fixtures. Nothing here touches the network or a real LLM API."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, Field

from scrapekit.config import ScraperConfig
from scrapekit.parsers.providers import StructuredClient

FIXTURES = Path(__file__).parent / "fixtures"


class Product(BaseModel):
    """A product listing."""

    name: str
    price: float = Field(description="Numeric price only, no currency symbol")
    currency: str = "USD"
    in_stock: bool = True
    tags: list[str] = Field(default_factory=list)


@pytest.fixture
def product_schema() -> type[Product]:
    return Product


@pytest.fixture
def product_html() -> str:
    return (FIXTURES / "product.html").read_text()


@pytest.fixture
def product_json() -> str:
    return (FIXTURES / "product.json").read_text()


@pytest.fixture
def challenge_html() -> str:
    return (FIXTURES / "challenge.html").read_text()


@pytest.fixture
def feed_xml() -> str:
    return (FIXTURES / "feed.xml").read_text()


@pytest.fixture
def config() -> ScraperConfig:
    """A config with the politeness delays disabled so tests run fast."""
    return ScraperConfig(
        strategies=["http"],
        respect_robots=False,
        min_delay=0.0,
        max_delay=0.0,
        max_retries=0,
        llm_api_key="test-key",
    )


class FakeStructuredClient(StructuredClient):
    """Stands in for a provider client and returns canned structured output.

    Records every call so tests can assert on the prompt that was built.
    """

    def __init__(self, result: Any = None, *, error: Exception | None = None) -> None:
        super().__init__(provider="fake", model="fake-model", client=None, default_kwargs={})
        self.result = result
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:  # type: ignore[override]
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        response_model = kwargs["response_model"]
        if isinstance(self.result, response_model):
            return self.result
        return response_model.model_validate(self.result)

    async def aclose(self) -> None:
        return None


@pytest.fixture
def fake_client_factory():
    return FakeStructuredClient
