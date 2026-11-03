<!-- SPDX-License-Identifier: Apache-2.0 -->
# sdo — Python SDK for Scrape.do

The official Python SDK for the [Scrape.do](https://scrape.do) web-scraping API.
Token-only auth, transparent compression, a typed result, and a generic plugin
core — no telemetry, no UI.

> Distribution name: **`sdo-sdk`** · import name: **`sdo`** · CLI binary (separate package): `sdo`.

## Install

```bash
pip install sdo-sdk
# optional CSS/XPath extraction via .selector():
pip install "sdo-sdk[parse]"
```

Requires Python 3.11+. Brotli decompression is bundled (the `brotli` dependency).

## 60-second quickstart

```python
from sdo import Client, ScrapeConfig

# Token precedence: Client(token=...) > SDO_TOKEN env var
with Client(token="YOUR_TOKEN") as client:
    result = client.scrape("https://example.com", render=True, geo_code="us")

    print(result.status_code)        # the TARGET's status code
    print(result.cost)               # credits this request cost
    print(result.text[:200])         # decoded HTML
    title = result.selector().css("title::text").get()

    # Reuse a config, or build one inline:
    cfg = ScrapeConfig(url="https://example.com", super_proxy=True, output="markdown")
    markdown = client.scrape(cfg).text
```

`ScrapeConfig` fields mirror the documented Scrape.do parameters. Values that
mean "unset" (`None`, `False`, `0`, `""`, empty list or dict) are not sent;
enum values are case-insensitive; the gateway timeouts are in
**milliseconds** (`timeout=30000`, range 5000–120000). Anything not modelled
yet goes through `extra_params={"someNewParam": "value"}`; a key must start
with an ASCII letter followed by letters, digits, `_`, `.` or `-`.

## Configuration

| Argument | Environment | Default |
| --- | --- | --- |
| `token` | `SDO_TOKEN` | required |
| `base_url` | `SDO_BASE_URL` | `https://api.scrape.do` |
| `async_base_url` | `SDO_ASYNC_BASE_URL` | `https://q.scrape.do` |
| `timeout` (seconds) | `SDO_TIMEOUT` | `70` |

An argument wins over its environment variable. `SDO_TIMEOUT` is a plain
decimal number of seconds of at least `0.001` (`30`, `1.5`); anything else is a
configuration error.

**Timeout.** `timeout` is a total wall-clock deadline per request attempt over
connect, headers and the whole body. Its default, 70 seconds, is the gateway's
own default timeout (60 seconds) plus 10, so the gateway's answer always
arrives first. httpx can only enforce it between reads (each read is bounded by
the same timeout), so a peer that trickles bytes can stretch one attempt to
about twice the timeout. When a scrape sets the gateway's own `timeout`
(milliseconds), or a plugin call a positive integer `timeout` param, that
attempt waits at least that plus 10 seconds, so the client never gives up on a
request the gateway was allowed to take longer for; the timeout error names the
value that applied.

**Retries.** `retries` (default 2) retries the retryable statuses (429, 502,
503, 504, 520–524) and failures before the request was sent (DNS, connect
refused, TLS, connect timeout). A failure after the request was sent — a read
timeout, a connection dropped mid-response, a body that cannot be decompressed
or is too large — is never retried, because the gateway may still run, and
bill, it. Requests that can change the target (POST, PUT, PATCH, DELETE) are
retried only on 429. A 510 is the gateway's record of a request whose HTTP
client cancelled it before it finished; it is not an upstream failure and is
not retried.

**Base URLs** are scheme, host, optional port and an optional path prefix,
which is kept (`https://gateway.internal/scrape-do` sends scrapes to
`/scrape-do/`). They must be `https://`; plain `http://` is accepted only for
`localhost`, `127.0.0.1` and `::1`, or with `allow_insecure_http=True` (the
token is then sent in cleartext). Credentials, a query or a fragment in a base
URL are rejected (the error message masks the credentials).

**Proxies and the environment.** The SDK's own `httpx` client is created with
`trust_env=False`: `HTTP(S)_PROXY`, `ALL_PROXY`, `NO_PROXY`, `SSL_CERT_FILE`,
`SSL_CERT_DIR` and `.netrc` are ignored, so a variable planted in the
environment cannot route your token through a proxy or make it trust another
CA. To use an outbound proxy or custom TLS settings, inject your own client,
which keeps its settings (redirects are still never followed):

```python
import httpx
from sdo import Client

with httpx.Client(proxy="http://proxy.internal:3128") as http, Client(http_client=http) as client:
    result = client.scrape("https://example.com")
```

## Authentication

The only thing you need is a Scrape.do token. It is resolved, in order:

1. `Client(token="...")` argument
2. `SDO_TOKEN` environment variable

Surrounding whitespace is trimmed (a token read from a secret file often ends
in a newline) and a blank value counts as not given; what is left must be
visible ASCII. The sync API receives it as the `token` query parameter and the
async job API as the `X-Token` header. It is masked (`***`) in every error
message, retained error body and `httpx`/`httpcore`/`hpack` log record, and no stack
frame that is live when the SDK raises holds it, so error trackers that record
local variables (Sentry, `pytest --showlocals`) do not capture it. No account
id, API key, or OAuth — just the token.

## Compression and limits

Every request sends `Accept-Encoding: gzip, deflate, br` and the response is
**transparently decompressed** — `result.bytes()` and `result.text` always give
you the decompressed payload. Pass `prefer_brotli=True` to advertise `br` alone.
Decompression is bounded: a body larger than `max_response_bytes` (default
32 MiB), compressed or not, raises `SdoResponseTooLargeError` before it is
inflated in memory. `Content-Encoding` may list several codings (`gzip`,
`x-gzip`, `deflate`, `br`), which are undone in reverse order; an unsupported
coding is left as received, and a truncated or corrupt stream is an
`SdoTransportError`. Redirects are never followed.

## Sending a body

```python
with Client() as client:
    client.scrape("https://example.com/api", json={"q": "laptop"})         # POST, JSON
    client.scrape("https://example.com/form", data="a=1&b=2")              # POST, raw
    client.scrape("https://example.com/item", method="PUT", json={"n": 1},
                  headers={"X-Api-Version": "2"}, forward_headers=True)
```

Header names are compared case-insensitively, and your value replaces the
SDK's own (`headers={"user-agent": "Mozilla/5.0 ..."}` sends that User-Agent
alone). Names must be HTTP tokens and values printable ASCII; `Host`,
`Content-Length`, `Transfer-Encoding` and `Connection` cannot be set. The SDK
sends no header of its own beyond `User-Agent`, `Accept-Encoding`,
`Content-Type` (with a body) and, for jobs, `X-Token`.

## Results

`result.text` decodes the body the way browsers do (the WHATWG Encoding
Standard: BOM, then the `Content-Type` charset, then `<meta charset>`, then
UTF-8). `result.headers` has lower-cased names; a repeated header such as
`Set-Cookie` is joined there, so read each value with
`result.header_values("set-cookie")`. `result.json()` parses the body as UTF-8
(invalid bytes become U+FFFD, a leading BOM is ignored) and raises
`SdoTransportError` for anything that is not JSON, including `NaN`, `Infinity`
and numbers too large for a double. With `disable_redirection=True`,
`result.target_redirected_location` is the target's `Location` (the
`Scrape.do-Target-Redirected-Location` header).

## Parameter rules

Besides enums, millisecond ranges and render-only options (`width` and
`height` included), the documented parameter dependencies are checked before
any request, each one the gateway rejects (or documents as invalid), so a
mistake costs nothing:

| Rule | Why |
|---|---|
| `show_frames`, `show_websocket_requests` and every screenshot option need `return_json` (and `render`) | their output is part of the JSON response |
| at most one of `screenshot`, `full_screenshot`, `particular_screenshot` | the gateway takes one |
| `particular_screenshot` cannot be combined with `play_with_browser` | use a `ScreenShot` action instead |
| `regional_geo_code` needs `super_proxy` and excludes `geo_code` | the gateway rejects both |
| `zip_code` needs `super_proxy` and `geo_code` | without super it is silently ignored |
| `session_id` is an integer 0–1000000 (an int or decimal digits) | the documented range |
| `retry_timeout` cannot be combined with `render` | the gateway rejects it |
| at most one of `custom_headers`, `extra_headers`, `forward_headers` | the gateway rejects any two |

`return_json` on its own is allowed: without `render` the gateway wraps the
plain response in the same JSON envelope. A target URL in an error message has
its `user:password@` masked.

## Errors

Successful scrapes — including pages that themselves return 404/410, and any
status the gateway billed — come back as a `ScrapeResult` (inspect
`result.status_code`). `Scrape.do-Request-Cost` is the authoritative billing
figure: a response of status 400 or above with a positive cost is the target's
own answer (its 400, 401, 403, 422, ...), returned as a result and never
retried. Without one, the status keeps its gateway meaning: the gateway's own
400 (a parameter it rejected, such as `render=mert`) raises
`SdoValidationError`, its 401 (a refused token, no credits) `SdoAuthError`. An
error's `billed` follows the cost header when present (0 is not billed) and
the documented status table otherwise; `error.cost` and `result.cost` hold the
header. Plugin calls follow the same rules, and a plugin's
`{"error": ..., "message": ...}` envelope supplies the message. Only *gateway-origin* failures raise, and every
failure is an `SdoError`:

```python
from sdo import SdoAuthError, SdoConcurrencyError, SdoError

with Client() as client:
    try:
        client.scrape("https://example.com")
    except SdoConcurrencyError as e:   # 429 — retryable
        print(e.retry_after)
    except SdoAuthError:               # 401/403 (also: no credits left)
        ...
    except SdoError as e:              # catch-all base
        print(e.message)
```

With `transparent_response=True` the gateway returns the target's own status;
such a response (it carries `Scrape.do-Initial-Status-Code`) is always returned
as a result, never raised.

## Plugins

Documented plugin endpoints are callable generically — a brand-new plugin works
the day it ships:

```python
with Client() as client:
    # Google plugins take Google's own locale params: gl (country), hl (language).
    data = client.plugin("google/shopping", {"q": "laptop", "gl": "us"})

    # plugin_result returns the same response with its status and headers.
    result = client.plugin_result("google/search", {"q": "laptop", "gl": "us"})
    print(result.status_code, result.cost, result.json())

    # The Amazon raw-HTML plugin lives at /plugin/amazon/; the route "amazon"
    # (or "amazon/") calls it.
    html = client.plugin(
        "amazon",
        {"url": "https://www.amazon.com/dp/B0C7BKZ883", "geocode": "us", "zipcode": "10001"},
    )
```

Parameters are sent sorted by name: a list repeats its name, a bool is
`true`/`false`, a dict is sent as JSON, `None` is skipped. A nested list, an
empty name, a parameter named `token` (in any case) or a name that is not an
ASCII letter followed by letters, digits, `_`, `.` or `-` is an
`SdoConfigError`.

## Usage statistics

```python
with Client() as client:
    usage = client.usage()  # GET https://api.scrape.do/info; no credits
    print(usage.is_active, usage.remaining_monthly_request, usage.remaining_concurrent_request)
```

`usage()` returns a `Usage` with `is_active`, `concurrent_request`,
`max_monthly_request`, `remaining_concurrent_request` and
`remaining_monthly_request`. The endpoint allows 10 requests per minute per IP
and answers 429 above that; the SDK does not retry its error statuses (a quick
retry only spends more of the same minute). An unknown token is answered with a
502 "authentication refused".

## Async jobs

Submit background scrapes via the async job REST API (`q.scrape.do/api/v1`,
authenticated with the `X-Token` header — handled for you). "Async" is the
name of the API (jobs run in the background); the methods themselves are
ordinary synchronous calls, not `asyncio` coroutines. They return typed
objects; a field the API sends with an unexpected type reads as `None`:

```python
from datetime import UTC, datetime

with Client() as client:
    job = client.jobs.create(["https://example.com"], {"Super": True, "GeoCode": "us"})
    status = client.jobs.status(job.job_id)
    for task in status.tasks:
        result = client.jobs.result(job.job_id, task.task_id)
        print(task.url, result.status_code, len(result.content_bytes()))

    # A plugin job: one task per params entry (at most 1000), with the plugin's
    # own parameter names. Targets and a plugin cannot be combined in one job.
    client.jobs.create_plugin(
        "amazon/pdp",
        [{"asin": "B0C7BKZ883", "geocode": "us"}, {"asin": "B0BSHF7WHW", "geocode": "us"}],
    )

    # The job list is paginated (default: the 10 most recent). Filter by status
    # and start time, sort, and walk `page` up to `total_pages`.
    page = client.jobs.list(
        status="success",
        start_from=datetime(2025, 1, 1, tzinfo=UTC),  # or an RFC 3339 string
        sort="start_time_asc",
        page=2,
        page_size=25,  # 1-100
    )
    page.jobs, page.total_count, page.total_pages

    client.jobs.cancel(job.job_id)
    client.jobs.me()              # concurrency + remaining credits
```

An invalid plugin key, an empty or oversized `params` list, options that set
`Targets` or `Plugin`, and `list` arguments outside the documented values raise
`SdoConfigError` before anything is sent. The async API has its own concurrency
pool (30% of the plan's limit, not taken from the main API's);
`JobStatus.acquired_concurrency` and `limit_concurrency` report a job's share of
it.

`client.jobs` shares the client's connection pool. For a job-only program,
`AsyncJobs(token=..., base_url=...)` builds its own, with the same validation
and environment fallbacks (use it in a `with` block).

> The old `callback` request parameter was removed by the gateway — use this job
> API instead.

## CI/CD workflows

| Workflow | Runs on | What it does |
|----------|---------|--------------|
| `ci.yml` | every push to `main` and every pull request | `ruff` lint and format check; `mypy --strict`; the test suite with the 99% line + branch coverage floor on Python 3.11–3.14 (Linux), plus macOS and Windows on 3.13, including the cross-language conformance suite and its integrity check; the public-API snapshot (`api-surface.txt`) must be up to date. CodeQL runs on every push and pull request. Everything runs on GitHub-hosted runners; CodeQL runs on the org's self-hosted runners for same-repository events (its upload is a GitHub API call the org's IP allow list only accepts from there); a pull request from a fork is analysed once merged. |
| `security.yml` | push to `main`, pull requests, and weekly | secret scanning over the full git history (gitleaks), OSV-Scanner over the pinned requirement files, `pip-audit`, and an SBOM uploaded as a build artifact. OpenSSF Scorecard runs on pushes to `main` and weekly, with results in code scanning. |
| `publish.yml` | a version tag only | A tag `vX.Y.Z-rc.N` publishes a release candidate to **TestPyPI**; a tag `vX.Y.Z` publishes to **PyPI**. The tag must be on `main` and match `src/sdo/_version.py` (`X.Y.ZrcN` for a candidate). The package is built once with hash-pinned tooling, checked with `twine check`, published by trusted publishing (no stored token) after approval in the `prerelease` or `release` environment, and then installed from the index as a smoke test. |
| Dependabot | weekly | update pull requests for the GitHub Actions, and security-advisory pull requests for the pinned Python requirements (version bumps of those are recompiled by hand with uv, see CONTRIBUTING.md), taken only after a 7-day cooldown; each one runs the checks above. |

Every action is pinned to a full commit SHA and every job declares least-privilege `permissions:`. Publishing never happens outside the `scrape-do` organisation, never from a commit that is not on `main`, and never without a maintainer's approval.

## Responsible use

You are responsible for complying with the terms of service, `robots.txt`, and
applicable laws (CFAA, GDPR, CCPA, etc.) of any site you scrape, and for not
collecting personal or copyrighted data unlawfully. Amazon, Walmart, Google and
other names are trademarks of their respective owners; Scrape.do is not
affiliated with them.

## License

[Apache-2.0](LICENSE).
