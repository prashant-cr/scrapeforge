# What and why

<!-- What changes, and what problem it solves. Link the issue if there is one. -->

Closes #

## How it was verified

<!--
Say what you actually ran, not what should pass. For a bug fix, the useful evidence is that the
new test fails without the fix — worth reverting your change once to check.
-->

## Checklist

- [ ] `pytest -m "not network"` passes
- [ ] `ruff check .` and `ruff format --check .` pass
- [ ] Tests added; a bug fix has a test that fails without the fix
- [ ] Any live test is marked `network` and uses a host that invites it
- [ ] No test reaches a real LLM API
- [ ] Public functions have type hints and a docstring
- [ ] New user-facing capability has an example under `examples/` and a README note

## Scope check

- [ ] No CAPTCHA solving, challenge bypass, or authentication bypass
- [ ] No logic targeting a specific site; challenge signatures stay vendor-neutral
- [ ] Optional dependencies stay optional — guarded import, `ConfigError` with an install hint
- [ ] No proxy credentials, cookies, or API keys can reach a log line or an exception message
- [ ] Fingerprint changes keep UA, headers, TLS, and browser engine in agreement
