# Contributing to scrapeforge

Thanks for taking the time. This document covers how to get set up, what the tests expect, and the
few conventions that keep the codebase coherent.

Participation is governed by the [Code of Conduct](CODE_OF_CONDUCT.md).

## Scope, before you invest time

scrapeforge is a general-purpose tool, and some things are deliberately out of scope. Knowing this
up front saves you writing a PR that gets declined on principle rather than on quality:

- **No CAPTCHA solving, and no defeating a specific vendor's challenge.** Challenges are detected
  and surfaced as `ChallengeError` so the caller decides what to do. Plugging in *your own* solver
  is your call; shipping one here is not.
- **No per-site logic.** Evasion stays generic and configurable. A fix that names a particular site
  belongs in your code, not this library. New challenge *signatures* are welcome — they live as
  data in `scrapeforge/utils/detect.py` and must be vendor-neutral.
- **No credential or paywall bypass.** Fingerprint and TLS handling exist for reliability against
  generic bot walls on otherwise-public content.
- **"Site X blocks me" is usually not a library bug.** It is the library working as designed. Open
  an issue if detection or escalation misbehaves; not if a site simply won that round.

Everything else — new fetchers, parsers, better preprocessing, docs, bug fixes — is fair game.

## Setup

```bash
git clone https://github.com/prashant-cr/scrapeforge
cd scrapeforge

uv venv && uv pip install -e ".[dev,llm,tls,browser]"
playwright install chromium        # only needed for the live browser tests
```

Plain `pip install -e ".[dev,llm,tls,browser]"` works too. Python 3.10–3.13 are supported and all
four are tested in CI.

## Tests

```bash
pytest                     # everything, including live network tests
pytest -m "not network"    # hermetic — what CI blocks on
pytest -m network          # only the live tests
```

The suite is hermetic by default in CI. Nineteen tests reach real hosts and are marked `network`;
they also skip themselves when no browser is installed.

**Where things go:**

| Kind of test | How it isolates | Example |
|---|---|---|
| HTTP behaviour | `respx` mocks | `test_http_fetcher.py` |
| Chain orchestration | scripted fake fetchers | `test_chain.py` |
| Parsers | committed fixtures under `tests/fixtures/` | `test_discovery_parsers.py` |
| LLM extraction | fake structured client | `test_parsers.py` |
| Provider wiring | real `instructor` + SDK, only HTTP mocked | `test_llm_integration.py` |
| Live sites | marked `network`, gated on a browser | `test_example_sites.py` |

**Never write a test that reaches a real LLM API.** Tests pass explicit fake keys, and CI blanks
`ANTHROPIC_API_KEY` / `OPENAI_API_KEY` so a stray credential cannot turn a mocked test into a billed
one. If you add a live test, mark it `network` and pick a host that invites it — `example.com`,
`books.toscrape.com`, `quotes.toscrape.com`, `postman-echo.com`.

A note on verifying packaging or installs: import from **outside** the repo directory, or the source
tree shadows the installed package and your check passes no matter what. Assert `site-packages` is in
`__file__` if it matters.

## Lint and format

```bash
ruff check . && ruff format .
```

CI pins ruff to the version in `.github/workflows/ci.yml`; an unpinned linter turns CI red on days
nobody touched the repo. If you bump it, bump the floor in `pyproject.toml` too.

## Conventions

- **Python 3.10+, full type hints, ruff clean.** The package ships `py.typed`, so annotations are
  part of the public contract.
- **Async-first.** Sync methods are thin wrappers that delegate — no duplicated logic.
- **Small, focused modules.** Don't collapse the fetchers into one file.
- **Google-style docstrings** on public classes and methods. Say *why* where the reason is not
  obvious from the code; the interesting comments in this codebase explain a decision, not a line.
- **Optional dependencies stay optional.** Guard the import and fail with a clear `ConfigError`
  ("install scrapeforge[browser]"), never an `ImportError` at import time. The `minimal-install` CI
  job enforces this.
- **Fail loud, fail typed.** Everything raised at the public API derives from `ScrapeforgeError`. A
  third-party library exception must never escape to the caller.
- **No secrets anywhere they can leak.** Proxy credentials, cookies, and API keys must not reach a
  log line or an exception message. Use `redact_proxy()`; API keys are `SecretStr`.
- **Add an example** under `examples/` for any new user-facing capability.

### Adding a fetch strategy

Implement `BaseFetcher`, give it a unique `name`, and register it:

```python
from scrapeforge import BaseFetcher, register_fetcher

@register_fetcher
class MyFetcher(BaseFetcher):
    name = "mine"
    requires = "some_package"          # optional dependency, if any
    extra_name = "scrapeforge[mine]"   # install hint used when it is missing

    async def fetch(self, url, options):
        ...  # return a FetchResponse
```

Translate the library's exceptions into the scrapeforge hierarchy, return a normalized
`FetchResponse`, and remember that judging whether a response is *usable* is the chain's job, not
the fetcher's — return the 403, don't raise on it.

### Fingerprint changes

Coherence is the point. A `User-Agent` must agree with the TLS fingerprint, the header set, the
browser engine, and the injected JS surface. If you add a user agent, add the metadata with it. Two
real bugs in this repo came from breaking that agreement — a Firefox UA on a Chromium engine, and
navigation headers stamped onto subresources — so tests asserting coherence are welcome.

## Pull requests

1. Branch from `main`.
2. Add tests. A bug fix should come with a test that fails without it — worth actually checking by
   reverting your fix.
3. `pytest -m "not network"` and `ruff check .` pass locally.
4. Keep the diff focused; unrelated cleanups in a separate PR.

CI runs lint, the 3.10–3.13 matrix, and the minimal-install check as blocking jobs. The live browser
job reports but does not gate, since an upstream outage there says nothing about your change.

## Releases

Maintainers only. Trusted Publishing over OIDC — no API tokens exist. Rehearse on TestPyPI, then tag
`vX.Y.Z` matching `pyproject.toml`; the workflow refuses to publish if they disagree. See the
Releasing section of the README.

## Security

Please do not open a public issue for a security problem. Use GitHub's private vulnerability
reporting on the repository's Security tab.

## Licence

By contributing you agree your contribution is licensed under the [MIT Licence](LICENSE).
