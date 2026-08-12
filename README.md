# scrapesmith

Schema-driven web scraping for Python. Give it a URL and a Pydantic model; it fetches through a
fallback chain of increasingly capable strategies and returns a validated instance of your model.

```python
from pydantic import BaseModel
from scrapesmith import Scraper


class Product(BaseModel):
    name: str
    price: float
    currency: str
    in_stock: bool


scraper = Scraper(llm_provider="anthropic", llm_model="claude-opus-5")
product = await scraper.scrape("https://shop.example/p/1", schema=Product)
```

- **Fallback chain.** Plain HTTP → TLS/JA3 impersonation → headless browser. Escalates when a
  response looks blocked, not just when the connection fails.
- **Schema-first.** Your Pydantic model *is* the contract. It is sent as a JSON schema, the output
  is constrained to it, and it is validated before you get it back.
- **Provider-agnostic.** OpenAI, Anthropic, Ollama, or any OpenAI-compatible endpoint.
- **Everything configurable.** Proxies, headers, cookies, timeouts, retries, strategy order — per
  client and per request.
- **Async-first**, with sync mirrors for callers who don't want async.

---

## Install

```bash
pip install scrapesmith                # core: httpx + curl_cffi + pydantic + selectolax
pip install "scrapesmith[browser]"     # + playwright
pip install "scrapesmith[llm]"         # + instructor / anthropic / openai
pip install "scrapesmith[tls]"         # + tls-client
pip install "scrapesmith[all]"
```

The browser strategy also needs its runtime downloaded once:

```bash
playwright install chromium
```

Optional dependencies stay optional. A strategy whose dependency is missing is skipped with a debug
log line, and an LLM SDK is only imported when you actually run an extraction.

---

## Usage

### 1. Structured scrape

```python
product = await scraper.scrape("https://shop.example/p/1", schema=Product)
```

### 2. Raw fetch, no parsing

```python
resp = await scraper.fetch(
    "https://api.example/search",
    method="POST",
    json={"q": "shoes"},
    proxy="http://other:port",
)

resp.content_type  # ContentType.JSON
resp.json()  # parsed JSON
resp.text  # raw text
resp.status_code
resp.strategy_used  # "http" | "impersonate" | "tls" | "browser"
```

### 3. Extract from content you already have

```python
product = await scraper.extract(resp.text, schema=Product, content_type="html")
```

### Sync

Every method has a `_sync` mirror that runs the async path in an event loop:

```python
resp = scraper.fetch_sync("https://example.com")
product = scraper.scrape_sync("https://shop.example/p/1", schema=Product)
```

They raise `ConfigError` if called from inside a running loop — use the async method there.

### Cleanup

The browser and HTTP clients are long-lived. Close them when you're done:

```python
async with Scraper(...) as scraper:
    ...
# or: await scraper.aclose()
```

---

## Configuration

```python
scraper = Scraper(
    # LLM
    llm_provider="anthropic",  # "openai" | "anthropic" | "ollama" | "openai_compatible"
    llm_model="claude-opus-5",
    llm_api_key=None,  # falls back to ANTHROPIC_API_KEY / OPENAI_API_KEY
    llm_base_url=None,  # required for "openai_compatible"
    # network defaults (overridable per call)
    proxy="http://user:pass@host:port",
    headers={"Accept-Language": "en-US,en;q=0.9"},
    cookies={"session": "abc"},
    timeout=30,
    max_retries=2,
    # strategy control
    strategies=["http", "impersonate", "browser"],  # order = escalation order
    impersonate_target="chrome124",
    browser_type="chromium",
    headless=True,
    rotate_user_agent=True,
    # responsible use
    respect_robots=True,
    max_concurrency_per_domain=4,
    min_delay=0.5,
    max_delay=1.5,
)
```

Per-request options override client defaults. `headers` and `cookies` are *merged* (per-request keys
win) rather than replaced, so adding one header does not discard your client defaults.

```python
await scraper.fetch(url, headers={"X-Trace": "1"}, timeout=60, strategies=["browser"])
```

---

## How fetching works

`FallbackChain` runs the enabled strategies in order:

| Strategy | Module | What it adds | Cost |
|---|---|---|---|
| `http` | `httpx` | HTTP/2, rotated UA, coherent headers | cheapest |
| `impersonate` | `curl_cffi` | Real browser TLS/JA3 + HTTP/2 frame ordering | low |
| `tls` | `tls-client` | Alternate impersonation profiles (opt-in) | low |
| `browser` | `playwright` | JS rendering, stealth patches | highest |

It moves to the next strategy when a response looks **blocked or challenged**, not only on hard
errors. Detection is generic and data-driven — the signature list lives in
`scrapesmith/utils/detect.py` and matches interstitial markers, WAF status codes paired with tiny or
HTML bodies, and content-type mismatches (you asked for JSON, you got an HTML block page).

**Retries vs. escalation are different things.** Transient network errors are retried on the same
strategy with exponential backoff and jitter. A block escalates immediately — retrying against a bot
wall wastes time and worsens the traffic pattern.

### Fingerprinting

User agents come from a bundled offline pool so tests are deterministic and a scrape never depends
on a third-party UA feed. Each entry carries the metadata needed to build a *coherent* header set:
brand list, platform, mobile flag, viewport, and a matching `curl_cffi` impersonation target.
Coherence is the whole point, and it is enforced in several places:

- A Chrome UA never ships with Firefox-only headers, and the TLS fingerprint always agrees with the
  `User-Agent`.
- `Accept-Encoding` only advertises encodings this install can actually decode. Asking for `br` and
  handing the caller raw Brotli bytes is worse than not asking — so `brotli` and `zstandard` are
  core dependencies, not extras.
- The browser fetcher picks a profile whose family matches the engine it launches. A Firefox UA on a
  Chromium engine is given away by the JS surface regardless of what the headers say.
- Chromium-only APIs (`window.chrome`, `navigator.deviceMemory`) are injected only for Chromium
  profiles. Defining them on a Firefox profile manufactures the exact inconsistency the evasions
  exist to remove.

Caller-supplied headers and a pinned `User-Agent` override all of this — user intent wins.

For the browser strategy, `fingerprint/stealth.py` normalizes `navigator.webdriver`,
`navigator.plugins`, `languages`, `hardwareConcurrency`, WebGL vendor/renderer, and the permissions
shape, and launches with flags that avoid automation give-aways.

One subtlety worth knowing: browsers wrap a JSON or XML document in `<html><body><pre>…</pre></body>`.
Since the chain can escalate an API request all the way to the browser, the browser fetcher returns
the **raw body** when the origin says the content is not HTML, and the **rendered DOM** when it is —
so `response.json()` works no matter which rung answered.

### Custom strategies

```python
from scrapesmith import BaseFetcher, register_fetcher


@register_fetcher
class MyFetcher(BaseFetcher):
    name = "mine"

    async def fetch(self, url, options): ...


Scraper(strategies=["mine", "browser"])
```

---

## How extraction works

1. **Detect** the content type from the `Content-Type` header, then by sniffing the body.
2. **Preprocess** for token economy — HTML is stripped of scripts, styles, comments and chrome and
   reduced to the main content region; JSON is compacted and long arrays truncated; XML is
   collapsed. Truncation is always marked explicitly, so a cut-off field never looks merely absent.
3. **Extract** via `instructor`, which constrains the output to your schema. On a validation failure
   it performs one bounded re-ask with the error fed back; if that also fails, `ParseError` is
   raised rather than returning a half-filled object.

Field descriptions from your model are forwarded to the extractor, so guidance belongs in the schema:

```python
class Product(BaseModel):
    price: float = Field(description="Numeric price only, no currency symbol")
```

### Non-LLM escape hatch

When the structure is known and stable, an LLM call is pure overhead:

```python
product = await scraper.scrape(
    url,
    schema=Product,
    parser="css",
    selectors={
        "name": "h1.title",
        "price": ".price@data-amount",  # attribute
        "tags": "ul.tags li[]",  # all matches, as a list
    },
)

# JSON APIs use dotted paths
await scraper.scrape(
    api_url,
    schema=Product,
    parser="jsonpath",
    selectors={
        "name": "product.title",
        "price": "product.offers.0.price",
    },
)
```

Same schema, same call shape — only the parser changes.

---

## Errors

```
ScrapesmithError
├── FetchError                 # all fetchers failed
│   ├── AllStrategiesFailed    # .attempts maps strategy -> reason
│   ├── ChallengeError         # blocked by bot management; .signature says what matched
│   └── ProxyError
├── ParseError                 # extraction/validation failed; .validation_error has the detail
└── ConfigError                # bad config, missing key, unknown strategy, robots disallow
```

Errors carry context — last status code, `strategy_used`, a truncated body snippet — without leaking
secrets. Proxy credentials, cookies, and API keys are never logged or included in messages; API keys
are held as `SecretStr` and proxy URLs are redacted before they reach a log line.

---

## Responsible use

**You are responsible for complying with each site's Terms of Service, applicable law, and
data-protection rules (including GDPR/CCPA where they apply).** Do not use scrapesmith to access data
you are not authorized to access, to overload servers, or to evade authentication.

Good behaviour is the default, and the defaults are deliberately conservative:

- **`robots.txt` is honored** (`respect_robots=True`). A disallowed URL raises `ConfigError`.
  Disabling the check is available and is your explicit, recorded choice.
- **Rate limiting is on.** Per-domain concurrency is capped (4) and requests to the same host are
  spaced by a randomized delay (0.5–1.5s). Both are per host, so a scrape across many domains is not
  throttled to the speed of one.
- **No CAPTCHA solving, no challenge bypass.** Challenges are detected and surfaced as
  `ChallengeError` so *you* decide what to do — plug in a solver, change proxy, or back off.
  scrapesmith will not do it for you.
- **No credential or paywall bypass.** Fingerprint and TLS handling exist for reliability against
  generic bot walls on otherwise-public content. They are not a tool for breaking into gated
  systems.
- **No per-site logic.** Evasion is generic and configurable; nothing here targets a particular
  site's or vendor's challenge.

---

## Development

```bash
uv venv && uv pip install -e ".[dev,browser,llm,tls]"
playwright install chromium

pytest                      # everything, including the live browser tests
pytest -m "not network"     # hermetic — no third-party host is touched
ruff check . && ruff format --check .
```

Tests never hit a real LLM API and never depend on a live site for their assertions:

- **Fetchers** mock HTTP with `respx` — header/UA rotation, proxy wiring, method and body handling.
- **Chain orchestration** uses scripted fake fetchers, so the tests cover the escalate-vs-retry
  decision rather than any one HTTP client.
- **`tls-client`** drives its own Go TLS stack, so `respx` cannot see its traffic. Those tests
  substitute a fake `tls_client` module and assert the seam instead: the parameters handed to the
  library, the response shape normalized back out, the errors translated, and that each session is
  closed. They run whether or not the optional extra is installed.
- **Parsers** use committed fixtures under `tests/fixtures/` with a fake structured client.
- **Provider wiring** (`test_llm_integration.py`) runs the genuine `LLMParser → instructor → vendor
  SDK` path with only the HTTP layer mocked, so a breaking SDK change is caught rather than hidden
  behind a fake. Skipped without the `llm` extra.
- **Browser** tests split: profile/engine coherence runs everywhere; the live rendering and stealth
  assertions are gated behind an installed browser.

### Learning by example

`tests/test_example_sites.py` is a guided walkthrough against real public sites, written to be read
as much as run. Each test is a small complete illustration of one capability — fetching, JSON APIs,
schema extraction, JS rendering, being blocked — asserted against a live site, so it shows what the
library actually does rather than what a fixture says it does:

```bash
pytest tests/test_example_sites.py -v
```

The sites are chosen deliberately: `example.com` (IANA-reserved for documentation),
`books.toscrape.com` and `quotes.toscrape.com` (published expressly as scraping sandboxes), and
`postman-echo.com` (a request echo service, which is the honest way to prove what actually goes on
the wire). Nothing there scrapes a site that didn't invite it, and the politeness controls stay on.

These and the live-browser tests are marked `network` — they skip when no browser is installed and
are deselected with `-m "not network"`.

### CI

`.github/workflows/ci.yml` runs four jobs on every push and pull request:

| Job | What it guards | Blocking |
|---|---|---|
| `lint` | `ruff check` + `ruff format --check` (ruff pinned, so a release can't turn CI red on its own) | yes |
| `test` | Hermetic suite on Python 3.10–3.13 | yes |
| `minimal-install` | Optional deps really are optional — imports work, missing strategies report unavailable instead of crashing, gated tests skip rather than fail | yes |
| `browser` | Live Chromium plus the example-sites walkthrough (`-m network`) | no — see below |

The browser job is `continue-on-error`. A red result there is as often an upstream outage or a
browser-download hiccup as a defect here, so it reports without gating; the hermetic jobs are the
real signal. Provider API keys are blanked at the workflow level so a runner-level credential can
never turn a mocked test into a billed one.

### Releasing

Publishing runs through `.github/workflows/release.yml` using PyPI
[Trusted Publishing](https://docs.pypi.org/trusted-publishers/) — GitHub Actions authenticates over
OIDC, so no API token exists in the repo, in CI, or on anyone's laptop.

One-time setup on PyPI and TestPyPI (Your projects → Publishing → Add a pending publisher):

| Field | Value |
|---|---|
| PyPI project name | `scrapesmith` |
| Owner | `prashant-cr` |
| Repository | `scrapesmith` |
| Workflow | `release.yml` |
| Environment | `pypi` (or `testpypi` on test.pypi.org) |

Then:

```bash
# 1. Rehearse. Actions → Release → Run workflow → target: testpypi
#    Install it somewhere clean and confirm it actually works:
pip install -i https://test.pypi.org/simple/ \
            --extra-index-url https://pypi.org/simple/ scrapesmith

# 2. Ship. The tag must match the version in pyproject.toml — the workflow
#    refuses to publish if they disagree.
git tag v0.1.0 && git push origin v0.1.0
```

The build runs once and both targets publish the *same* artifacts, so the rehearsal is byte-for-byte
what lands on PyPI. A PyPI version number can be yanked but never reused, so the workflow validates
metadata with `twine check --strict` and installs the built wheel to confirm it imports before
anything is uploaded.

## License

MIT — see [LICENSE](LICENSE).
