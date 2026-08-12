# CLAUDE.md

> Guidance for Claude Code when building and maintaining **scrapekit** — an open-source, schema-driven web scraping library with layered anti-bot evasion and LLM-based extraction.
>
> Rename `scrapekit` to whatever the project owner prefers; it is used throughout as a placeholder.

---

## 1. What we are building

`scrapekit` is a Python library that:

1. Takes a URL and returns whatever the server serves — HTML, JSON, or XML — for both `GET` and `POST` requests.
2. Accepts a **Pydantic schema** describing the fields to extract, and uses an LLM to parse the fetched content into a validated instance of that schema.
3. Fetches content through a **fallback chain of strategies**, escalating from cheap/fast plain HTTP up to a full stealth browser, applying user-agent rotation, realistic headers, and TLS/JA3 fingerprint impersonation to get past common bot-management stacks (Cloudflare, DataDome, PerimeterX).
4. Lets the caller pass **proxies, custom headers, and custom cookies** on every request.

The library is a general-purpose tool. Design it to encourage responsible use (see §11). Do not hardcode logic that targets any specific site or that solves a specific site's CAPTCHA/challenge; keep evasion generic and configurable.

---

## 2. Core design principles

- **Strategy pattern for fetching.** Every fetch method implements a common `BaseFetcher` interface. A `FallbackChain` tries them in order until one returns a "good" response.
- **Schema-first extraction.** The user's Pydantic model *is* the contract. The parser's job is to fill it and validate it.
- **Provider-agnostic LLM layer.** Never hardcode a single model vendor. Route through an abstraction so OpenAI, Anthropic, or a local model (Ollama / any OpenAI-compatible endpoint) all work.
- **Everything is configurable, nothing is magic.** Proxy, headers, cookies, timeouts, retries, which strategies to enable, and which LLM to use are all explicit config.
- **Sync + async.** Public API is async-first with a thin sync wrapper. Playwright and httpx both support async natively.
- **Fail loud, fail typed.** Use a small hierarchy of custom exceptions rather than leaking library-internal errors.

---

## 3. Tech stack

| Concern | Library | Notes |
|---|---|---|
| Plain HTTP | `httpx` | async, HTTP/2, clean API |
| Browser impersonation + TLS/JA3 | `curl_cffi` | impersonates Chrome/Safari/Firefox TLS fingerprints |
| Alt TLS client | `tls-client` | fallback impersonation option |
| Headless browser | `playwright` | Chromium/Firefox/WebKit |
| Stealth patches | `playwright-stealth` (or a maintained fork) + custom init scripts | mask `navigator.webdriver`, etc. |
| Schema / validation | `pydantic` v2 | user-facing schema + internal config models |
| LLM structured output | `instructor` (wraps providers) or `litellm` for routing | returns validated Pydantic objects |
| HTML/XML pre-processing | `selectolax` (fast) or `lxml` | trim DOM before sending to the LLM |
| UA data | `fake-useragent` or a bundled UA pool | keep an offline pool as fallback |
| Retry/backoff | `tenacity` | on the chain and per-strategy |
| Packaging | `pyproject.toml` + `hatchling` or `uv` | |
| Lint/format | `ruff` + `ruff format` | |
| Tests | `pytest` + `pytest-asyncio` + `respx` | mock HTTP; use local fixture pages for parsing tests |

Keep `playwright` and the LLM providers as **optional extras** so a minimal install stays light:

```
pip install scrapekit                # core: httpx + curl_cffi + pydantic
pip install "scrapekit[browser]"     # + playwright
pip install "scrapekit[llm]"         # + instructor/litellm
pip install "scrapekit[all]"
```

---

## 4. Repository layout

```
scrapekit/
├── scrapekit/
│   ├── __init__.py            # exports Scraper, ScraperConfig, exceptions
│   ├── client.py             # Scraper: the public entrypoint
│   ├── config.py             # ScraperConfig, FetchOptions (pydantic)
│   ├── models.py             # FetchResponse, ContentType enum
│   ├── exceptions.py
│   ├── fetchers/
│   │   ├── base.py           # BaseFetcher ABC
│   │   ├── http.py           # HttpxFetcher (simple GET/POST)
│   │   ├── impersonate.py    # CurlCffiFetcher (TLS/JA3)
│   │   ├── tls.py            # TlsClientFetcher
│   │   ├── browser.py        # PlaywrightFetcher (stealth)
│   │   └── chain.py          # FallbackChain orchestrator
│   ├── fingerprint/
│   │   ├── user_agents.py    # UA pool + rotation
│   │   ├── headers.py        # realistic header set builder
│   │   └── stealth.py        # playwright evasion init scripts
│   ├── parsers/
│   │   ├── base.py           # BaseParser ABC
│   │   ├── llm.py            # LLMParser (schema -> instance)
│   │   ├── preprocess.py     # DOM/JSON trimming for token economy
│   │   └── providers.py      # provider routing (openai/anthropic/ollama)
│   └── utils/
│       └── detect.py         # sniff HTML vs JSON vs XML; challenge heuristics
├── tests/
├── examples/
│   ├── simple_html.py
│   ├── json_api_post.py
│   └── schema_extraction.py
├── pyproject.toml
├── README.md
├── LICENSE                   # MIT or Apache-2.0
└── CLAUDE.md
```

---

## 5. Public API (target shape)

```python
from pydantic import BaseModel
from scrapekit import Scraper

class Product(BaseModel):
    name: str
    price: float
    currency: str
    in_stock: bool

scraper = Scraper(
    # LLM config
    llm_provider="anthropic",          # "openai" | "anthropic" | "ollama" | "openai_compatible"
    llm_model="claude-sonnet-4-6",
    # network defaults (overridable per call)
    proxy="http://user:pass@host:port",
    headers={"Accept-Language": "en-US,en;q=0.9"},
    cookies={"session": "abc"},
    timeout=30,
    # strategy control
    strategies=["http", "impersonate", "browser"],  # order = escalation order
    max_retries=2,
)

# 1) Structured scrape: fetch + LLM extraction into the schema
product = await scraper.scrape("https://shop.example/p/1", schema=Product)

# 2) Raw fetch, no parsing
resp = await scraper.fetch("https://api.example/search", method="POST",
                           json={"q": "shoes"}, proxy="http://other:port")
resp.content_type   # ContentType.JSON
resp.json()         # parsed JSON
resp.text           # raw text
resp.status_code
resp.strategy_used  # which fetcher succeeded

# 3) Extract from already-fetched content (no network)
product = await scraper.extract(resp.text, schema=Product, content_type="html")
```

Provide a synchronous mirror (`scraper.scrape_sync`, `scraper.fetch_sync`) that runs the async path in an event loop for users who don't want async.

---

## 6. Config models (`config.py`)

Define these as Pydantic models so validation and defaults live in one place.

- `ScraperConfig`: `llm_provider`, `llm_model`, `llm_api_key` (env fallback), `strategies: list[str]`, `proxy`, `headers`, `cookies`, `timeout`, `max_retries`, `impersonate_target` (e.g. `"chrome124"`), `browser_type` (`"chromium"`), `headless: bool`, `rotate_user_agent: bool`.
- `FetchOptions`: per-request overrides — `method`, `params`, `data`, `json`, `headers`, `cookies`, `proxy`, `timeout`, `strategies`. Merge order: per-call `FetchOptions` overrides `ScraperConfig` defaults.

Read secrets (LLM keys) from env by default (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`) and allow explicit override. **Never log full proxy credentials, cookies, or API keys.**

---

## 7. Fetching: the fallback chain (`fetchers/`)

### 7.1 `BaseFetcher` interface

```python
class BaseFetcher(ABC):
    name: str
    @abstractmethod
    async def fetch(self, url: str, options: FetchOptions) -> FetchResponse: ...
    def is_available(self) -> bool: ...   # e.g. playwright installed?
```

Each fetcher returns a normalized `FetchResponse` (status, headers, body bytes, resolved content type, final URL, `strategy_used`).

### 7.2 Escalation order

The `FallbackChain` runs enabled strategies in order. It moves to the next strategy when a response looks **blocked or challenged**, not just on hard errors.

Default order:

1. **`HttpxFetcher`** — plain async HTTP. Fast, cheap. Rotates user agent, sends a realistic header set. Handles `GET`/`POST` (form data + JSON). Good enough for open APIs and simple pages.
2. **`CurlCffiFetcher`** — `curl_cffi` with `impersonate="chrome124"` (or configured target). This matches a real browser's TLS/JA3 fingerprint and HTTP/2 frame ordering, which clears a large share of fingerprint-based blocks. Supports proxies, custom headers/cookies, POST.
3. **`TlsClientFetcher`** — alternate TLS impersonation via `tls-client`. Optional second impersonation option when `curl_cffi` doesn't match a needed profile.
4. **`PlaywrightFetcher`** — real headless browser with stealth (see §8). Slowest and heaviest; used only when the lighter strategies are blocked or when the page needs JS to render. Supports proxy, extra headers, cookie injection, and waiting for network idle / selectors.

Make the order configurable via `strategies`. If a strategy's dependency isn't installed (`is_available()` false), skip it and log a debug note.

### 7.3 "Is this response usable?" heuristics (`utils/detect.py`)

Before accepting a response, check for common challenge signatures and escalate if found:

- HTTP status in `{403, 429, 503}` combined with a tiny body or a known challenge marker.
- Body contains generic interstitial markers (e.g. "Just a moment", `cf-chl`, a DataDome/PerimeterX challenge script tag).
- Expected content type mismatch (asked for JSON, got an HTML challenge page).

Keep these heuristics **generic and data-driven** (a list of signature strings in one module), not per-site special cases. If none match and the status is 2xx, accept.

### 7.4 Retries

Wrap each strategy in `tenacity` with exponential backoff + jitter for transient network errors (timeouts, connection resets). Distinguish *retryable* (network) from *escalate* (blocked) — a block should advance the chain, not burn retries on the same fetcher.

---

## 8. Fingerprint handling (`fingerprint/`)

### 8.1 User agents & headers (`user_agents.py`, `headers.py`)

- Maintain an offline UA pool (recent Chrome/Firefox/Safari desktop + mobile). Optionally refresh via `fake-useragent`, but always have the bundled pool as fallback so tests are deterministic and offline-safe.
- When `rotate_user_agent` is on, pick a UA per request and build a **matching, coherent header set** — `Sec-CH-UA`, `Sec-Fetch-*`, `Accept`, `Accept-Language`, `Accept-Encoding` consistent with that UA. Incoherent headers (Chrome UA with Firefox-only headers) are a red flag to bot managers; keep them internally consistent.
- Let user-supplied headers override generated ones (user intent wins).

### 8.2 TLS / JA3

- Primary mechanism is `curl_cffi`'s `impersonate` target — it handles the TLS ClientHello, cipher ordering, and HTTP/2 settings to match the impersonated browser. Expose `impersonate_target` in config.
- `tls-client` is the secondary path when a specific profile is needed that `curl_cffi` doesn't offer.

### 8.3 Playwright stealth (`stealth.py`)

When using the browser fetcher, apply evasions via init scripts / launch args:

- Remove/override `navigator.webdriver`.
- Normalize `navigator.plugins`, `languages`, `hardwareConcurrency`, `deviceMemory`.
- Consistent WebGL vendor/renderer and canvas behavior.
- Realistic viewport + `User-Agent` + `Accept-Language` matching the chosen UA.
- Launch with args that avoid automation give-aways; run non-headless-detectable where feasible.
- Support waiting strategies: `networkidle`, specific selector, or a timeout, configurable per call.

Keep evasion generic. Do **not** implement automated CAPTCHA solving or logic that defeats a specific vendor's proof-of-work challenge — surface a clear `ChallengeError` instead and let the caller decide (e.g. plug in their own solver or proxy).

---

## 9. Parsing & extraction (`parsers/`)

### 9.1 Flow

1. **Detect content type** (from `Content-Type` header, then body sniffing): HTML / JSON / XML.
2. **Preprocess** (`preprocess.py`) to keep token usage sane:
   - HTML: strip `<script>`, `<style>`, comments, and boilerplate; optionally reduce to the main content region or a relevant subtree. Consider converting to simplified text/markdown for the LLM.
   - JSON/XML: pass through, optionally trimmed to relevant keys if the payload is huge.
3. **LLM structured extraction** (`llm.py`): call the provider with the preprocessed content and the target Pydantic schema, using `instructor` (or an equivalent structured-output mechanism) so the return is a **validated instance of the user's schema**. On validation failure, do one bounded re-ask with the validation error fed back, then raise `ParseError`.

### 9.2 Provider abstraction (`providers.py`)

- Single `get_client(provider, model, api_key)` that returns an `instructor`-patched client for `openai`, `anthropic`, `ollama`, or any `openai_compatible` base URL.
- The parser only depends on this abstraction, never on a concrete SDK.
- Make the extraction prompt small and generic: "Extract the fields defined by this schema from the following content. Use null for anything genuinely absent; do not invent values." Field descriptions from the Pydantic model (`Field(description=...)`) should be forwarded to guide extraction.

### 9.3 Non-LLM escape hatch (nice-to-have)

Allow a `parser="css"`/`parser="jsonpath"` mode where the schema fields map to CSS/XPath/JSONPath selectors for deterministic, token-free extraction when the structure is known. LLM parsing is the default; this is an optimization.

---

## 10. Exceptions (`exceptions.py`)

```
ScrapekitError                 # base
├── FetchError                 # all fetchers failed
│   ├── AllStrategiesFailed
│   ├── ChallengeError         # blocked by bot management / challenge page
│   └── ProxyError
├── ParseError                 # LLM extraction/validation failed
└── ConfigError                # bad config (missing key, unknown strategy)
```

Attach useful context (last status code, `strategy_used`, truncated body snippet) without leaking secrets.

---

## 11. Responsible-use guardrails (build these in)

This is an open-source tool; make good behavior easy and default:

- **`robots.txt` awareness.** Provide a `respect_robots: bool = True` option that checks and honors `robots.txt`; when the user disables it, that's their explicit choice.
- **Rate limiting.** Built-in per-domain concurrency limit and configurable delay/jitter between requests; default to polite values.
- **Clear docs.** README must state that users are responsible for complying with each site's Terms of Service, applicable law, and data-protection rules, and must not use the library to access data they're not authorized to access, overload servers, or evade authentication.
- **No credential/paywall bypass.** Do not build features whose only purpose is defeating authentication or access controls. Fingerprint/TLS handling is for reliability against generic bot walls on otherwise-public content, not for breaking into gated systems.

Treat these as first-class requirements, not afterthoughts.

---

## 12. Testing

- **Fetchers:** mock HTTP with `respx`; assert header/UA rotation, proxy wiring, method/body handling, and that challenge heuristics trigger escalation. Gate Playwright tests behind an installed-browser marker.
- **Parsers:** use committed local HTML/JSON/XML fixtures under `tests/fixtures/`. Mock the LLM client to return canned structured output so parser tests are deterministic and don't hit a real API. Add a small optional integration test (skipped without an API key) for a real end-to-end extraction.
- **Detection:** unit-test content-type sniffing and challenge-signature matching against fixture bodies.
- Target meaningful coverage on the chain-orchestration and preprocessing logic — that's where the bugs live.

---

## 13. Conventions for Claude Code

- Python **3.10+**, full type hints, `ruff` clean.
- Async-first; sync wrappers delegate, no duplicated logic.
- Small, focused modules matching §4. Don't collapse fetchers into one file.
- Google-style docstrings on public classes/methods; keep the README examples runnable.
- Optional dependencies stay optional — guard imports and fail with a clear `ConfigError` ("install scrapekit[browser]") rather than an `ImportError` at import time.
- Every new fetcher implements `BaseFetcher` and registers with the chain by name.
- No secrets in logs. No per-site hardcoding. Keep evasion generic and configurable.
- Add an example under `examples/` for any new user-facing capability.

---

## 14. Suggested build order

1. `config.py`, `models.py`, `exceptions.py`, `utils/detect.py`.
2. `fingerprint/` (UA pool + header builder).
3. `HttpxFetcher` + `CurlCffiFetcher` + `FallbackChain` (get raw fetching working with proxy/headers/cookies and escalation).
4. `parsers/` (preprocess + provider abstraction + LLM extraction).
5. Wire `Scraper` public API (`fetch`, `scrape`, `extract`) + sync wrappers.
6. `PlaywrightFetcher` + stealth.
7. `TlsClientFetcher`.
8. Responsible-use options (`robots`, rate limiting), tests, examples, README.

Ship a working core (steps 1–5) before adding the browser layer.
