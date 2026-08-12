"""Parsers: LLM extraction (with a faked client) and deterministic selectors."""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from scrapekit.exceptions import ConfigError, ParseError
from scrapekit.models import ContentType
from scrapekit.parsers.llm import LLMParser
from scrapekit.parsers.selector import SelectorParser


class TestLLMParser:
    async def test_returns_a_validated_instance(
        self, config, product_html, product_schema, fake_client_factory
    ):
        client = fake_client_factory(
            {"name": "Trail Runner GTX", "price": 149.95, "currency": "USD", "in_stock": True}
        )
        parser = LLMParser(config, client=client)

        product = await parser.parse(product_html, product_schema)

        assert isinstance(product, product_schema)
        assert product.name == "Trail Runner GTX"
        assert product.price == 149.95

    async def test_sends_preprocessed_content_not_raw_html(
        self, config, product_html, product_schema, fake_client_factory
    ):
        client = fake_client_factory({"name": "x", "price": 1.0})
        parser = LLMParser(config, client=client)

        await parser.parse(product_html, product_schema)

        sent = client.calls[0]["user"]
        assert "dataLayer" not in sent, "scripts must be stripped before the call"
        assert "<span" not in sent
        assert "Trail Runner GTX" in sent

    async def test_forwards_schema_name_and_docstring(
        self, config, product_html, product_schema, fake_client_factory
    ):
        client = fake_client_factory({"name": "x", "price": 1.0})
        parser = LLMParser(config, client=client)

        await parser.parse(product_html, product_schema)

        sent = client.calls[0]["user"]
        assert "Product" in sent
        assert "A product listing." in sent

    async def test_system_prompt_forbids_invention(
        self, config, product_html, product_schema, fake_client_factory
    ):
        client = fake_client_factory({"name": "x", "price": 1.0})
        parser = LLMParser(config, client=client)

        await parser.parse(product_html, product_schema)

        system = client.calls[0]["system"].lower()
        assert "null" in system
        assert "do not invent" in system

    async def test_instructions_are_passed_through(
        self, config, product_html, product_schema, fake_client_factory
    ):
        client = fake_client_factory({"name": "x", "price": 1.0})
        parser = LLMParser(config, client=client)

        await parser.parse(product_html, product_schema, instructions="Prefer the sale price.")

        assert "Prefer the sale price." in client.calls[0]["user"]

    async def test_json_content_is_trimmed_not_stripped_as_html(
        self, config, product_json, product_schema, fake_client_factory
    ):
        client = fake_client_factory({"name": "x", "price": 1.0})
        parser = LLMParser(config, client=client)

        await parser.parse(product_json, product_schema, content_type=ContentType.JSON)

        assert '"title":"Trail Runner GTX"' in client.calls[0]["user"]

    async def test_bounded_reask_is_requested(
        self, config, product_html, product_schema, fake_client_factory
    ):
        client = fake_client_factory({"name": "x", "price": 1.0})
        parser = LLMParser(config, client=client)

        await parser.parse(product_html, product_schema)

        assert client.calls[0]["max_retries"] == 1

    async def test_validation_failure_raises_parse_error(
        self, config, product_html, product_schema, fake_client_factory
    ):
        class Wrong(BaseModel):
            required_field: int

        try:
            Wrong.model_validate({})
        except ValidationError as exc:
            error = exc

        client = fake_client_factory(error=error)
        parser = LLMParser(config, client=client)

        with pytest.raises(ParseError) as exc_info:
            await parser.parse(product_html, product_schema)

        assert exc_info.value.schema == "Product"
        assert exc_info.value.validation_error is not None

    async def test_provider_failure_raises_parse_error(
        self, config, product_html, product_schema, fake_client_factory
    ):
        client = fake_client_factory(error=RuntimeError("upstream 503"))
        parser = LLMParser(config, client=client)

        with pytest.raises(ParseError, match="upstream 503"):
            await parser.parse(product_html, product_schema)

    async def test_empty_content_fails_before_spending_a_call(
        self, config, product_schema, fake_client_factory
    ):
        client = fake_client_factory({"name": "x", "price": 1.0})
        parser = LLMParser(config, client=client)

        with pytest.raises(ParseError, match="empty"):
            await parser.parse("   \n  ", product_schema)

        assert client.calls == []

    async def test_client_is_built_lazily(self, config, product_schema):
        """Importing scrapekit must never require an LLM SDK."""
        parser = LLMParser(config)
        assert parser._client is None


class TestSelectorParserCss:
    async def test_extracts_text_attributes_and_lists(self, product_html, product_schema):
        parser = SelectorParser(
            {
                "name": "h1.title",
                "price": ".price@data-amount",
                "currency": ".price@data-currency",
                "in_stock": ".availability@data-in-stock",
                "tags": "ul.tags li[]",
            }
        )

        product = await parser.parse(product_html, product_schema)

        assert product.name == "Trail Runner GTX"
        assert product.price == 149.95
        assert product.currency == "USD"
        assert product.in_stock is True
        assert product.tags == ["waterproof", "trail", "gore-tex"]

    async def test_missing_selector_yields_none_and_uses_schema_default(
        self, product_html, product_schema
    ):
        parser = SelectorParser(
            {"name": "h1.title", "price": ".price@data-amount", "tags": ".nope[]"}
        )

        product = await parser.parse(product_html, product_schema)

        assert product.tags == []

    async def test_invalid_result_raises_parse_error(self, product_html, product_schema):
        parser = SelectorParser({"name": "h1.title", "price": ".brand"})  # brand is not numeric

        with pytest.raises(ParseError) as exc_info:
            await parser.parse(product_html, product_schema)

        assert exc_info.value.validation_error is not None

    async def test_css_on_json_is_rejected(self, product_json, product_schema):
        parser = SelectorParser({"name": "h1"}, mode="css")

        with pytest.raises(ConfigError, match="cannot be applied to JSON"):
            await parser.parse(product_json, product_schema, content_type=ContentType.JSON)


class TestSelectorParserJsonPath:
    async def test_dotted_paths_with_indices_and_lists(self, product_json, product_schema):
        parser = SelectorParser(
            {
                "name": "product.title",
                "price": "product.offers.0.price",
                "currency": "product.offers.0.currency",
                "in_stock": "product.offers.0.inStock",
                "tags": "product.tags[]",
            }
        )

        product = await parser.parse(product_json, product_schema, content_type=ContentType.JSON)

        assert product.name == "Trail Runner GTX"
        assert product.price == 149.95
        assert product.tags == ["waterproof", "trail", "gore-tex"]

    async def test_missing_path_is_none(self, product_json, product_schema):
        parser = SelectorParser(
            {
                "name": "product.title",
                "price": "product.offers.0.price",
                "currency": "product.nope.deep",
            }
        )

        product = await parser.parse(product_json, product_schema, content_type=ContentType.JSON)

        assert product.currency == "USD"  # schema default

    async def test_auto_mode_picks_path_for_json(self, product_json, product_schema):
        parser = SelectorParser({"name": "product.title", "price": "product.offers.0.price"})

        product = await parser.parse(product_json, product_schema, content_type=ContentType.JSON)

        assert product.name == "Trail Runner GTX"

    async def test_invalid_json_raises_parse_error(self, product_schema):
        parser = SelectorParser({"name": "a"}, mode="jsonpath")

        with pytest.raises(ParseError, match="not valid JSON"):
            await parser.parse("{broken", product_schema, content_type=ContentType.JSON)

    def test_empty_selectors_rejected(self):
        with pytest.raises(ConfigError):
            SelectorParser({})

    def test_unknown_mode_rejected(self):
        with pytest.raises(ConfigError):
            SelectorParser({"a": "b"}, mode="xpath")
