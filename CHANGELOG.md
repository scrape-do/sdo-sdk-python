<!-- SPDX-License-Identifier: Apache-2.0 -->
# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/) · [SemVer](https://semver.org/).

Before 1.0, a minor version may contain breaking changes; each is marked
**Breaking** and described in MIGRATING.md. Patch versions never break.

## [Unreleased]

## [0.1.0] - 2026-11-18

The first release of `sdo-client`, the official Python SDK for the Scrape.do API
(import name `sdo`). Requires Python 3.11 or newer.

### Added

- **`Client`** — token-only auth: `Client(token=...)`, else `SDO_TOKEN`. The
  token is trimmed of surrounding whitespace, a blank value counts as not given,
  and it must be visible ASCII. `SDO_BASE_URL`, `SDO_ASYNC_BASE_URL` and
  `SDO_TIMEOUT` are read when the matching argument is not given. Usable as a
  context manager.
- **Base URLs** are scheme, host, optional port and an optional path prefix
  (kept on every request). They must be `https://`, except for `localhost`,
  `127.0.0.1` and `::1` or with `allow_insecure_http=True`; credentials, a
  query or a fragment are rejected, and the error message masks any
  credentials it was given.
- **Timeouts** — `timeout` (seconds, default 70: the gateway's 60-second
  default plus 10; `SDO_TIMEOUT` is a decimal number of at least 0.001) is a
  total wall-clock deadline per attempt over connect, headers and body,
  enforced between reads, so a peer that trickles bytes can stretch one attempt
  to about twice the timeout. A scrape that sets the gateway `timeout`
  (milliseconds), or a plugin call a positive integer `timeout` param, gets at
  least that plus 10 seconds.
- **Retries** — `retries` (default 2) with exponential backoff and jitter,
  honouring `Retry-After` (whole seconds, capped at 30). Retried: 429, 502, 503,
  504 and 520–524, and failures before the request was sent (DNS, connect,
  TLS, connect timeout). Never retried: a failure after the request was sent
  (read timeout, dropped connection, an undecodable or oversized body), and a
  510, the gateway's record of a request its HTTP client cancelled before it
  finished (default message "The request was canceled by the HTTP client (HTTP
  510)."). POST/PUT/PATCH/DELETE are retried only on 429.
- **`ScrapeConfig`** — every documented scrape parameter as a field; `url` may
  be positional, every other field is keyword-only. Unset values (`None`,
  `False`, `0`, `""`, empty list or dict) are omitted and never validated,
  except that `block_resources` is tri-state: `None` sends nothing, `True`
  needs `render`, and `False` is sent as `blockResources=false` (the gateway
  defaults to `true`). Render-only parameters (`width` and `height` included)
  require `render`; enums are validated
  case-insensitively and sent lowercase; the documented millisecond ranges are
  enforced (`timeout` 5000–120000, `retry_timeout` 5000–55000, `custom_wait`
  0–35000); `set_cookies` takes the
  raw cookie string; `extra_params` sends any other key verbatim (`token` and
  `url` are reserved; a key is an ASCII letter followed by letters, digits,
  `_`, `.` or `-`). The target URL must be an absolute `http`/`https` URL
  with a host (no whitespace, control characters or backslashes) and is
  otherwise sent exactly as given.
- **Parameter rules** — the documented parameter dependencies the gateway
  rejects with a 400, or documents as invalid, raise `SdoConfigError` before
  any request: `show_frames`, `show_websocket_requests` and the screenshot
  options require `return_json`; at most one screenshot option;
  `particular_screenshot` excludes `play_with_browser`; `regional_geo_code`
  requires `super_proxy` and excludes `geo_code`; `zip_code` requires
  `super_proxy` and `geo_code`; `session_id` is an integer 0–1000000 (an `int`
  or decimal digits; `0` is sent); `retry_timeout` excludes `render`; at most
  one of `custom_headers`, `extra_headers`, `forward_headers`. `return_json`
  without `render` is valid (the gateway wraps the plain response in JSON).
- **One canonical wire format**, identical in every Scrape.do SDK: the query
  string is built by the SDK (typed parameters in a fixed order, then
  `extra_params` sorted; RFC 3986 escaping), and JSON values and bodies use
  sorted keys and ECMAScript number formatting.
- **`Client.scrape(..., method=, json=, data=, headers=)`** — GET by default,
  POST with a body; `json=` is sent as canonical JSON with
  `Content-Type: application/json` unless you set one. Header names are
  compared case-insensitively and your value replaces the SDK's own; names
  must be HTTP tokens and values printable ASCII; `Host`, `Content-Length`,
  `Transfer-Encoding` and `Connection` cannot be set. The SDK sends no header
  of its own beyond `User-Agent` (exactly `sdo-sdk-python/<version>`),
  `Accept-Encoding`, `Content-Type` and, for jobs, `X-Token`. A rendered
  request (`render=True`) with any method other than GET raises
  `SdoConfigError` before anything is sent: the gateway answers it with a 400
  ("JavaScript Render feature just works with 'GET' method type").
- **`ScrapeResult`** — `.bytes()`, `.text` (decoded as the WHATWG Encoding
  Standard specifies: BOM, `Content-Type` charset, JSON as UTF-8, the HTML
  `<meta>` prescan, UTF-8), `.json()` (UTF-8 with invalid bytes replaced; `NaN`,
  `Infinity` and out-of-range numbers rejected) and `.selector()` (optional
  `parsel`); lower-cased `headers`, `header_values(name)` for repeated headers,
  and typed metadata (`cost`, `remaining_credits`, `initial_status_code`,
  `target_url`, `resolved_url`, `cookies`, `content_type`,
  `target_redirected_location` — the target's `Location` under
  `disable_redirection`).
- **Transparent decompression** — every request sends
  `Accept-Encoding: gzip, deflate, br` (`prefer_brotli=True` sends `br`
  alone). Stacked codings are undone in reverse order (`gzip`, `x-gzip`,
  `deflate`, `br`); an unsupported coding is left as received; a truncated or
  corrupt stream is an `SdoTransportError`.
- **Errors** — one `SdoError` taxonomy: `SdoConfigError`, `SdoTransportError`
  (`SdoConnectionError`, `SdoTimeoutError`, `SdoResponseTooLargeError`) and
  `SdoAPIError` (`SdoValidationError` 400, `SdoAuthError` 401/403 — also an
  exhausted credit balance, `SdoConcurrencyError` 429, `SdoGatewayError` 5xx).
  API errors carry `status_code`, `billed`, `is_retryable`, `retry_after` and
  the credential-masked `body` (first 2048 characters). The
  message comes from the error envelope when there is one, else a fixed default
  message (`message`, then `error`, each in any letter case, so a plugin's
  `{"error": ..., "message": ...}` reads too); an error body is read up to 64
  KiB, and one that is larger, corrupt or cut off still raises the error for
  its status. A target's own 404/410, and any status of 400 or above the
  gateway billed (a positive `Scrape.do-Request-Cost`, the authoritative
  figure), is a result, not an error, and is never retried; with
  `transparent_response=True`, so is any status the target returned. Without a
  positive cost a status keeps its gateway meaning (the gateway's own 400 is an
  `SdoValidationError`, its 401 an `SdoAuthError`, both with `billed=False`),
  on the scrape and plugin paths alike. `billed` follows the cost header when
  present, else the documented table; `SdoAPIError.cost` holds the header,
  read like `ScrapeResult.cost` (one value, ASCII digits, at most 2**53 - 1).
- **Plugins** — `client.plugin(route, params)` calls any documented plugin
  endpoint generically and returns its JSON (or the raw bytes). Routes are
  validated; parameters are sent sorted, lists repeat their key, booleans as
  `true`/`false`, dicts as canonical JSON; a nested list, an empty key, a
  `token` key or a key that is not an ASCII letter followed by letters,
  digits, `_`, `.` or `-` is a config error. `client.plugin_result(route,
  params)` returns the same response as a `ScrapeResult`, so its
  `status_code` and `cost` are not lost. The route `amazon` (or its documented
  spelling `amazon/`) calls the Amazon raw-HTML plugin at `/plugin/amazon/`,
  the path the gateway serves it under (it answers `/plugin/amazon` with a
  redirect the SDK never follows).
- **Usage statistics** — `client.usage()` reads `GET /info` on the sync base
  URL into a `Usage` (`is_active`, `concurrent_request`, `max_monthly_request`,
  `remaining_concurrent_request`, `remaining_monthly_request`); a body without
  those fields raises `SdoTransportError`. The endpoint is rate-limited to 10
  requests per minute, so its error statuses (429 included) are not retried.
- **Async jobs** — `client.jobs` (or a standalone `AsyncJobs`) for the job API
  at `q.scrape.do/api/v1`: `create`, `create_plugin`, `status`, `result`,
  `list`, `cancel` and `me`, returning typed `Job`, `JobStatus` (with
  `acquired_concurrency` and `limit_concurrency`), `JobPage`, `Task`,
  `TaskResult` and `Account` objects. `create_plugin(key, params,
  options=None)` submits a plugin job (`{"Plugin": {"Key": ..., "Params":
  [...]}}`), one task per params entry, 1 to 1000 entries; a malformed key, an
  empty or oversized list, or options that set `Targets` or `Plugin` (on
  `create` too) raise `SdoConfigError`. `list(page=, page_size=, status=,
  start_from=, start_to=, sort=)` (page size 1-100; start bounds as an aware
  `datetime` or an RFC 3339 string) reads one page of the paginated job list
  into a `JobPage` (`jobs`, `total_count`, `page_size`, `page_number`,
  `total_pages`); `JOB_STATUSES`, `JOB_SORTS` and `MAX_PLUGIN_PARAMS` export
  the accepted values. The token travels in the `X-Token` header; job and task
  ids are validated before they enter the URL; a field of an unexpected type
  reads as absent; `TaskResult.content_bytes()` decodes base64 content.

### Security

- `brotli` is required at 1.2.1 or newer: earlier versions have an
  out-of-bounds write in the decoder (PYSEC-2026-4322, GHSA-9v8h-qqgm-4m35),
  reachable from a target's `Content-Encoding: br` response.
- A target URL in an `SdoConfigError` has its `user:password@` masked and its
  credential-shaped query values redacted.
- The token is never logged or echoed: it is masked in every error message,
  retained error body and `httpx`/`httpcore`/`hpack` log record, and no stack
  frame that is live when the SDK raises holds it (error trackers that record
  local variables do not capture it). A `KeyboardInterrupt` or other
  `BaseException` raised mid-request propagates unchanged, with the frames
  below the SDK that held the request (and its token URL) cleared.
- The SDK's own `httpx` client is created with `trust_env=False`: proxy,
  CA-bundle and `.netrc` environment variables are ignored, so a planted
  `HTTPS_PROXY` cannot route the token elsewhere. Configure a proxy by
  injecting an `http_client`, which keeps its own settings.
- URLs are matched as ASCII: a scheme such as `httpſ://` (long s) is not
  case-folded into `https://`.
- The token only travels over HTTPS unless you opt in for a non-loopback host.
- Decompression is bounded on every path: the raw read and each decoding step
  stop at `max_response_bytes` (default 32 MiB) before the payload is inflated
  in memory.
- A body is decoded through at most five `Content-Encoding` codings (urllib3's
  limit); a longer chain is a `SdoTransportError`, never retried, so a target
  cannot make the SDK stack a decoder per listed coding.
- A GBK or GB18030 page decodes in memory proportional to its size, with no
  backtracking regex over the body.
- Redirects are never followed, including through an injected `http_client`.
- Only Encoding Standard labels select a decoder, so a page cannot pick an
  arbitrary Python codec.
- CI: code from a fork's pull request never reaches the organisation's
  persistent self-hosted runners: every job runs on GitHub-hosted runners
  except CodeQL (same-repository events only; a fork's pull request is
  analysed once merged) and Scorecard (never on a pull request); every
  checkout drops its token
  (`persist-credentials: false`); and gitleaks scans with a config and ignore
  list from the workflow, so a pull request cannot allowlist its own secret with
  a `.gitleaks.toml` or `.gitleaksignore`.
- No telemetry and no request you did not make.

[Unreleased]: https://github.com/scrape-do/sdo-sdk-python/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/scrape-do/sdo-sdk-python/releases/tag/v0.1.0
