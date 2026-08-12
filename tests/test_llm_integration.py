"""The real provider stack — instructor + the vendor SDK — over mocked HTTP.

The unit tests in ``test_parsers.py`` fake the structured client, which proves
the parser's own logic but not that our provider wiring matches the SDK. These
tests exercise the genuine path (LLMParser -> instructor -> vendor SDK -> HTTP)
with only the network mocked, so a breaking SDK change is caught here.

Skipped when the ``llm`` extra is not installed.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from scrapeforge.config import ScraperConfig
from scrapeforge.exceptions import ParseError
from scrapeforge.parsers.llm import LLMParser

instructor = pytest.importorskip("instructor", reason="requires scrapeforge[llm]")

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"

EXTRACTED = {
    "name": "Trail Runner GTX",
    "price": 149.95,
    "currency": "USD",
    "in_stock": True,
    "tags": ["waterproof", "trail", "gore-tex"],
}


def anthropic_response(payload: dict, *, tool_name: str = "Product") -> httpx.Response:
    """A Messages API response carrying structured output as a tool call."""
    return httpx.Response(
        200,
        json={
            "id": "msg_01",
            "type": "message",
            "role": "assistant",
            "model": "claude-opus-5",
            "content": [
                {"type": "tool_use", "id": "toolu_01", "name": tool_name, "input": payload}
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 120, "output_tokens": 30},
        },
    )


def openai_response(payload: dict, *, tool_name: str = "Product") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-01",
            "object": "chat.completion",
            "created": 0,
            "model": "gpt-4o-mini",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_01",
                                "type": "function",
                                "function": {
                                    "name": tool_name,
                                    "arguments": json.dumps(payload),
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150},
        },
    )


@pytest.fixture
def anthropic_config() -> ScraperConfig:
    return ScraperConfig(llm_provider="anthropic", llm_api_key="sk-test", max_content_chars=20_000)


@pytest.fixture
def openai_config() -> ScraperConfig:
    return ScraperConfig(llm_provider="openai", llm_model="gpt-4o-mini", llm_api_key="sk-test")


class TestAnthropicPath:
    @respx.mock
    async def test_returns_a_validated_instance(
        self, anthropic_config, product_html, product_schema
    ):
        respx.post(ANTHROPIC_URL).mock(return_value=anthropic_response(EXTRACTED))
        parser = LLMParser(anthropic_config)

        product = await parser.parse(product_html, product_schema)
        await parser.aclose()

        assert isinstance(product, product_schema)
        assert product.name == "Trail Runner GTX"
        assert product.price == 149.95
        assert product.tags == ["waterproof", "trail", "gore-tex"]

    @respx.mock
    async def test_request_shape(self, anthropic_config, product_html, product_schema):
        route = respx.post(ANTHROPIC_URL).mock(return_value=anthropic_response(EXTRACTED))
        parser = LLMParser(anthropic_config)

        await parser.parse(product_html, product_schema)
        await parser.aclose()

        body = json.loads(route.calls[0].request.content)
        assert body["model"] == "claude-opus-5"
        # max_tokens is required by the Anthropic API and also covers thinking.
        assert body["max_tokens"] == anthropic_config.llm_max_tokens
        assert body["system"], "the system prompt goes in the top-level field, not messages"
        assert [m["role"] for m in body["messages"]] == ["user"]

    @respx.mock
    async def test_schema_is_sent_with_field_descriptions(
        self, anthropic_config, product_html, product_schema
    ):
        """The user's model is the contract — it must reach the provider intact."""
        route = respx.post(ANTHROPIC_URL).mock(return_value=anthropic_response(EXTRACTED))
        parser = LLMParser(anthropic_config)

        await parser.parse(product_html, product_schema)
        await parser.aclose()

        body = json.loads(route.calls[0].request.content)
        schema = body["tools"][0]["input_schema"]
        assert set(schema["properties"]) == {"name", "price", "currency", "in_stock", "tags"}
        assert "Numeric price only" in schema["properties"]["price"]["description"]

    @respx.mock
    async def test_content_is_preprocessed_before_it_is_sent(
        self, anthropic_config, product_html, product_schema
    ):
        route = respx.post(ANTHROPIC_URL).mock(return_value=anthropic_response(EXTRACTED))
        parser = LLMParser(anthropic_config)

        await parser.parse(product_html, product_schema)
        await parser.aclose()

        sent = json.loads(route.calls[0].request.content)["messages"][0]["content"]
        sent_text = sent if isinstance(sent, str) else json.dumps(sent)
        assert "Trail Runner GTX" in sent_text
        assert "dataLayer" not in sent_text, "scripts must not reach the provider"
        assert len(sent_text) < len(product_html)

    @respx.mock
    async def test_bounded_reask_then_parse_error(
        self, anthropic_config, product_html, product_schema
    ):
        """Invalid output is retried once, then fails loudly rather than half-filled."""
        route = respx.post(ANTHROPIC_URL).mock(
            return_value=anthropic_response({"name": "x", "price": "not-a-number"})
        )
        parser = LLMParser(anthropic_config)

        with pytest.raises(ParseError) as exc_info:
            await parser.parse(product_html, product_schema)
        await parser.aclose()

        assert exc_info.value.schema == "Product"
        assert route.call_count >= 2, "one bounded re-ask should have been attempted"

    @respx.mock
    async def test_api_error_becomes_parse_error(
        self, anthropic_config, product_html, product_schema
    ):
        respx.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(
                401,
                json={
                    "type": "error",
                    "error": {"type": "authentication_error", "message": "bad key"},
                },
            )
        )
        parser = LLMParser(anthropic_config)

        with pytest.raises(ParseError):
            await parser.parse(product_html, product_schema)
        await parser.aclose()


class TestOpenAIPath:
    @respx.mock
    async def test_returns_a_validated_instance(self, openai_config, product_html, product_schema):
        respx.post(OPENAI_URL).mock(return_value=openai_response(EXTRACTED))
        parser = LLMParser(openai_config)

        product = await parser.parse(product_html, product_schema)
        await parser.aclose()

        assert product.name == "Trail Runner GTX"
        assert product.price == 149.95

    @respx.mock
    async def test_system_prompt_is_a_message_not_a_field(
        self, openai_config, product_html, product_schema
    ):
        """OpenAI takes the system prompt in `messages`; Anthropic takes a field."""
        route = respx.post(OPENAI_URL).mock(return_value=openai_response(EXTRACTED))
        parser = LLMParser(openai_config)

        await parser.parse(product_html, product_schema)
        await parser.aclose()

        body = json.loads(route.calls[0].request.content)
        assert [m["role"] for m in body["messages"]] == ["system", "user"]
        assert "system" not in body
        assert body["model"] == "gpt-4o-mini"
