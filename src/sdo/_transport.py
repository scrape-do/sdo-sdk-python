# SPDX-License-Identifier: Apache-2.0
"""HTTP transport: the one request path for scrape, plugins and async jobs.

Every request goes through :meth:`Transport.send`, so every path shares the
same guarantees:

* ``Accept-Encoding`` is explicit and the body is decompressed here, bounded:
  the raw read and every decoding step stop at ``max_response_bytes`` before
  the inflated buffer is materialized (see :mod:`sdo._decompress`);
* the client timeout is a total wall-clock deadline per attempt over connect,
  headers and the whole body. httpx can only enforce it between reads (each
  read is bounded by the same timeout), so a peer that trickles bytes can
  stretch one attempt to about twice the timeout;
* redirects are never followed, whatever the injected ``httpx.Client`` says;
* URLs are absolute and the query string is built by the SDK, byte-exact;
* the headers sent are exactly the SDK's own plus the caller's, compared
  case-insensitively; httpx's defaults are not sent;
* every failure is an :class:`~sdo.errors.SdoError` whose message, retained
  body and (absent) cause carry no credential, and no frame that is live when
  it is raised holds the token;
* retries follow one policy: retryable statuses (only 429 for methods that can
  have side effects on the target), and failures before anything was sent
  (idempotent methods only). A failure after the request was sent is never
  retried: the gateway may still run, and bill, it.
"""

from __future__ import annotations

import random
import re
import time
import traceback
import weakref
from collections.abc import Mapping
from dataclasses import dataclass, field

import httpx

from . import _json, _logging
from ._decompress import BodyDecoder, DecodeError, TooLarge
from ._secret import Secret
from ._wire import pct
from .errors import (
    SdoAPIError,
    SdoAuthError,
    SdoConcurrencyError,
    SdoConfigError,
    SdoConnectionError,
    SdoError,
    SdoGatewayError,
    SdoResponseTooLargeError,
    SdoTimeoutError,
    SdoTransportError,
    SdoValidationError,
    redact_secrets,
)
from .response import parse_request_cost

#: Statuses where the same request may succeed when retried. 510 is not one:
#: the gateway answers 510 only when the HTTP client cancelled the request
#: before the gateway finished it, so a retry would re-send an abandoned request.
_RETRYABLE_STATUS = frozenset({429, 502, 503, 504, 520, 521, 522, 523, 524})
#: The gateway's status for a request its HTTP client cancelled.
_CLIENT_CANCELED_STATUS = 510
#: Methods that can have side effects on the target: retried only on 429, which
#: the gateway returns before contacting the target.
_NON_IDEMPOTENT = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_NON_IDEMPOTENT_RETRY_STATUS = frozenset({429})
#: The documented billing table, used only for an error without a readable
#: ``Scrape.do-Request-Cost`` (the header is authoritative): of the statuses
#: that can be in an error position, only 404 and 410, which are results
#: anyway. The gateway's own 400, 401, 429, 502 and 510 are not billed.
_BILLED_STATUS = frozenset({404, 410})
#: Default ceiling for a response body, raw and decoded (32 MiB).
_MAX_BYTES = 32 * 1024 * 1024
#: How much of an error body is read: enough for any envelope.
_ERROR_BODY_BYTES = 64 * 1024
_MAX_BACKOFF = 30.0
_RETAINED_BODY_CHARS = 2048
#: With a gateway ``timeout`` set, one attempt waits at least that plus this.
_GATEWAY_TIMEOUT_MARGIN = 10
#: With transparent_response, this header marks the target's own answer.
_TARGET_ORIGIN_HEADER = "scrape.do-initial-status-code"
#: The credits the gateway charged for the request.
_REQUEST_COST_HEADER = "scrape.do-request-cost"

SYNC = "sync"
ASYNC = "async"
#: Where the token travels: the ``token`` query parameter, or ``X-Token``.
QUERY = "query"
HEADER = "header"

_DEFAULT_MESSAGES = {
    "validation": "The gateway rejected the request parameters.",
    "auth": "Authentication failed — check your token, plan permissions, and remaining credits.",
    "concurrency": "Concurrent-request limit reached.",
    "gateway": "Gateway-origin upstream failure (HTTP {status}).",
    "canceled": "The request was canceled by the HTTP client (HTTP {status}).",
    "api": "Request failed (HTTP {status}).",
    "redirect": "Unexpected redirect (HTTP {status}); redirects are never followed.",
}

_DIGITS_RE = re.compile(r"[0-9]+", re.ASCII)
_SIGNED_RE = re.compile(r"-?[0-9]+", re.ASCII)
#: RFC 9110 token.
_HEADER_NAME_RE = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
#: Printable ASCII plus space and tab.
_HEADER_VALUE_RE = re.compile(r"[\t\x20-\x7e]*")
#: Managed by the HTTP client; a caller value would corrupt the framing.
_FORBIDDEN_HEADERS = frozenset({"host", "content-length", "transfer-encoding", "connection"})
#: Of the headers httpx puts on a built request, only these framing ones stay.
_FRAMING_HEADERS = frozenset({"host", "content-length", "transfer-encoding"})


@dataclass
class Reply:
    """A complete response: status, every header as received, decoded body."""

    status_code: int
    headers: list[tuple[str, str]]
    body: bytes = field(default=b"")
    #: False when an error body could not be read whole (it failed to
    #: decompress, dropped or timed out): its envelope is then not trusted.
    body_intact: bool = True

    def header(self, name: str) -> str | None:
        name = name.lower()
        for k, v in self.headers:
            if k.lower() == name:
                return v
        return None


def retry_after_seconds(raw: str | None) -> int | None:
    """``Retry-After`` as whole seconds (RFC 9110 delay-seconds), else ``None``.

    Only ASCII digits count: a date, a negative, a fraction, ``NaN`` or
    ``'١٢'`` fall back to the exponential backoff.
    """
    if raw is None:
        return None
    raw = raw.strip(" \t")
    return int(raw) if _DIGITS_RE.fullmatch(raw) else None


def effective_timeout(client_seconds: float, config_timeout_ms: int | None) -> float:
    """The per-attempt timeout: with a gateway ``timeout`` (milliseconds) set,
    at least that plus a margin, so the client never gives up on a request the
    gateway was told it may take that long for."""
    if not config_timeout_ms:
        return client_seconds
    return max(client_seconds, config_timeout_ms / 1000 + _GATEWAY_TIMEOUT_MARGIN)


def _int_or_none(raw: str | None) -> int | None:
    if raw is None:
        return None
    raw = raw.strip(" \t")
    return int(raw) if _SIGNED_RE.fullmatch(raw) else None


def _ascii_lower(text: str) -> str:
    """Lowercase A-Z only (``str.lower`` would also fold ``"İ"`` or a Kelvin sign)."""
    return text.translate(_ASCII_LOWER)


_ASCII_LOWER = {c: c + 32 for c in range(ord("A"), ord("Z") + 1)}


def _envelope_text(value: object) -> str | None:
    if isinstance(value, list):
        return "; ".join(m for m in value if isinstance(m, str) and m) or None
    if isinstance(value, str):
        return value or None
    return None


def _sync_envelope(data: object) -> str | None:
    """A ``message`` key, else an ``error`` key, each matched in any ASCII
    letter case: the gateway writes ``{"Message": ["...", ...]}``, a plugin
    ``{"error": "code", "message": "text"}``. The value is a string or a list
    (string elements joined with ``"; "``). Several keys matching one name are
    tried in byte order (``"Message"`` before ``"message"``); one that yields
    nothing falls through to the next, then to ``error``."""
    if not isinstance(data, dict):
        return None
    keys = sorted(k for k in data if isinstance(k, str))
    for name in ("message", "error"):
        for key in keys:
            if _ascii_lower(key) == name:
                text = _envelope_text(data[key])
                if text is not None:
                    return text
    return None


def _async_envelope(data: object) -> str | None:
    """``{"Error": "...", "Code": n}``: only ``Error`` is read."""
    if isinstance(data, dict):
        error = data.get("Error")
        if isinstance(error, str) and error:
            return error
    return None


def envelope_message(body: bytes, api: str = SYNC) -> str | None:
    """The message an error body carries (a leading BOM is ignored)."""
    try:
        data = _json.loads(body)
    except ValueError:
        return None
    return _async_envelope(data) if api == ASYNC else _sync_envelope(data)


def _classify(status: int) -> tuple[type[SdoAPIError], str]:
    if status in (401, 403):
        # The gateway returns 401 for a depleted credit balance too, so the
        # default message names both causes.
        return SdoAuthError, "auth"
    if status == 429:
        return SdoConcurrencyError, "concurrency"
    if status == 400:
        return SdoValidationError, "validation"
    if status >= 500:
        return SdoGatewayError, "gateway"
    if 300 <= status < 400:
        return SdoAPIError, "redirect"
    return SdoAPIError, "api"


def build_api_error(reply: Reply, *, api: str = SYNC, token: str | None = None) -> SdoAPIError:
    """Map an error response to the taxonomy. The message comes from the error
    envelope when there is one; message and retained body are redacted, then
    the body is truncated."""
    status = reply.status_code
    cls, kind = _classify(status)
    if status == _CLIENT_CANCELED_STATUS:
        kind = "canceled"  # still an SdoGatewayError; not an upstream failure
    envelope = envelope_message(reply.body, api) if reply.body_intact else None
    message = redact_secrets(envelope or _DEFAULT_MESSAGES[kind].format(status=status), token)
    # The cost header is authoritative; the documented table is the fallback.
    cost = request_cost(reply) if api == SYNC else None
    billed = api == SYNC and (status in _BILLED_STATUS if cost is None else cost > 0)
    retryable = status in _RETRYABLE_STATUS
    retry_after = retry_after_seconds(reply.header("retry-after"))
    text = reply.body.decode("utf-8", errors="replace")
    body = redact_secrets(text, token)[:_RETAINED_BODY_CHARS]
    if cls is SdoConcurrencyError:
        return SdoConcurrencyError(
            message,
            status_code=status,
            billed=billed,
            is_retryable=retryable,
            retry_after=retry_after,
            body=body,
            remaining_credits=_int_or_none(reply.header("scrape.do-remaining-credits")),
            cost=cost,
        )
    return cls(
        message,
        status_code=status,
        billed=billed,
        is_retryable=retryable,
        retry_after=retry_after,
        body=body,
        cost=cost,
    )


def is_result(reply: Reply, *, api: str, transparent: bool) -> bool:
    """Whether a response is a delivered result rather than an error."""
    status = reply.status_code
    if api == ASYNC:
        return 200 <= status < 300
    if transparent and reply.header(_TARGET_ORIGIN_HEADER) is not None:
        return True  # the target's own answer, whatever its status
    if target_answered(reply):
        return True
    return status < 400 or status in (404, 410)


def request_cost(reply: Reply) -> int | None:
    """``Scrape.do-Request-Cost`` read as :attr:`ScrapeResult.cost` reads it:
    exactly one value, ASCII digits, at most 2**53 - 1; else ``None``."""
    values = [v for k, v in reply.headers if k.lower() == _REQUEST_COST_HEADER]
    return parse_request_cost(values[0]) if len(values) == 1 else None


def target_answered(reply: Reply) -> bool:
    """Whether the gateway billed the response, which makes it the target's
    answer whatever its status.

    The gateway charges the target statuses it counts as delivered (400, 401,
    404, 405 and more) and states the charge in ``Scrape.do-Request-Cost``; its
    own errors — a parameter it cannot parse (``render=mert``), a refused token,
    a blocked URL — are written before any cost is known and carry no cost
    header, and a failed fetch carries a cost of 0. A billed response is a
    result, so it is never retried. Without a readable positive cost the status
    keeps its gateway meaning, so a gateway message is never passed off as the
    target's page.
    """
    cost = request_cost(reply)
    return cost is not None and cost > 0


def merge_headers(*layers: Mapping[str, str] | None) -> dict[str, str]:
    """Validate header layers and merge them case-insensitively: a later
    layer's value replaces an earlier one's (never duplicated or joined)."""
    merged: dict[str, tuple[str, str]] = {}
    for layer in layers:
        if layer is None:
            continue
        if not isinstance(layer, Mapping):
            raise SdoConfigError("headers must be a mapping of header names to str values.")
        for name, value in layer.items():
            if not isinstance(name, str) or not _HEADER_NAME_RE.fullmatch(name):
                raise SdoConfigError(f"Invalid header name {name!r}: it must be an HTTP token.")
            if name.lower() in _FORBIDDEN_HEADERS:
                raise SdoConfigError(
                    f"The {name!r} header cannot be set: the HTTP client manages it."
                )
            if not isinstance(value, str) or not _HEADER_VALUE_RE.fullmatch(value):
                raise SdoConfigError(
                    f"Invalid value for header {name!r}: it must be a str of printable ASCII "
                    "(no CR, LF, NUL or other control characters)."
                )
            merged[name.lower()] = (name, value)
    return dict(merged.values())


def _wire_headers(built: httpx.Headers, ours: Mapping[str, str]) -> httpx.Headers:
    """Only the framing headers httpx computed, plus ours: its defaults
    (Accept, Connection, its own User-Agent) are not sent."""
    framing = [(k, v) for k, v in built.raw if k.lower().decode() in _FRAMING_HEADERS]
    return httpx.Headers([*framing, *((k.encode(), v.encode()) for k, v in ours.items())])


def _clear_frames_below(exc: BaseException) -> None:
    """Clear every finished frame below the current one in ``exc``'s
    traceback (and in its chained exceptions'), keeping the type, message and
    the traceback's shape; a frame still executing is skipped."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        tb = current.__traceback__
        traceback.clear_frames(tb.tb_next if tb is not None and current is exc else tb)
        current = current.__cause__ or current.__context__


@dataclass
class _Failure:
    """An attempt that produced no usable response."""

    error: SdoError
    #: True when the request provably never left the client (DNS, connect,
    #: TLS, connect timeout): only then is it safe to send it again.
    unsent: bool = False


class _Stop(Exception):
    """Carries a :class:`_Failure` out of the body read."""

    def __init__(self, failure: _Failure) -> None:
        super().__init__(failure.error.message)
        self.failure = failure


class Transport:
    """Owns the httpx client and the request / retry / error-mapping loop.

    The token is held as a :class:`~sdo._secret.Secret` and added to the request
    (query or ``X-Token``) only inside :meth:`_attempt`, which has returned by
    the time :meth:`send` raises.
    """

    def __init__(
        self,
        *,
        token: Secret,
        timeout: float,
        accept_encoding: str,
        retries: int,
        backoff_factor: float,
        user_agent: str,
        max_response_bytes: int = _MAX_BYTES,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._token = token
        self._timeout = timeout
        self._accept_encoding = accept_encoding
        self._retries = max(0, retries)
        self._backoff = backoff_factor
        self._ua = user_agent
        self._max_bytes = max_response_bytes
        self._owns_client = http_client is None
        # No base_url: every request carries an absolute URL, so an injected
        # client cannot re-route the sync or the async API. The default client
        # ignores the environment (HTTPS_PROXY, ALL_PROXY, SSL_CERT_FILE,
        # .netrc): a variable planted there would otherwise route the token
        # through a proxy or trust a foreign CA. An injected client keeps its
        # own settings, which is how a proxy is configured.
        self._client = http_client or httpx.Client(follow_redirects=False, trust_env=False)
        self._closed = False
        _logging.register_token(token.reveal())
        # Unregister on close(), or when the transport is garbage-collected.
        self._forget_token = weakref.finalize(self, _logging.unregister_token, token.reveal())

    # -- public ----------------------------------------------------------------
    def send(
        self,
        method: str,
        url: str,
        *,
        query: str = "",
        auth: str = QUERY,
        headers: Mapping[str, str] | None = None,
        content: bytes | None = None,
        api: str = SYNC,
        transparent: bool = False,
        timeout: float | None = None,
        gateway_timeout_ms: int | None = None,
        retry_status: bool = True,
    ) -> Reply:
        """Send a request through the retry loop and return the complete reply,
        or raise the mapped :class:`~sdo.errors.SdoError`.

        ``url`` is the absolute URL without a query; ``query`` is the encoded
        query string without the token, which ``auth`` places. Without
        ``timeout``, an attempt waits the client's timeout, raised to
        ``gateway_timeout_ms`` (a plugin's ``timeout`` param) plus the margin.
        ``retry_status=False`` keeps a retryable status from being retried (a
        failure before anything was sent still is): for ``/info``, whose 429 is
        a per-minute rate limit a quick retry only extends.
        """
        if self._closed or self._client.is_closed:
            raise SdoConfigError(
                "This client is closed. Create a new Client, or use it inside its `with` block."
            )
        sdk_headers = {"User-Agent": self._ua, "Accept-Encoding": self._accept_encoding}
        req_headers = merge_headers(sdk_headers, headers)
        idempotent = method not in _NON_IDEMPOTENT
        per_attempt = (
            effective_timeout(self._timeout, gateway_timeout_ms) if timeout is None else timeout
        )

        attempt = 0
        while True:
            outcome = self._attempt(
                method, url, query, auth, req_headers, content, api, transparent, per_attempt
            )
            can_retry = attempt < self._retries
            if isinstance(outcome, _Failure):
                if can_retry and outcome.unsent and idempotent:
                    self._sleep(attempt, None)
                    attempt += 1
                    continue
                raise outcome.error
            if is_result(outcome, api=api, transparent=transparent):
                return outcome
            err = build_api_error(outcome, api=api, token=self._token.reveal())
            allowed = _RETRYABLE_STATUS if idempotent else _NON_IDEMPOTENT_RETRY_STATUS
            if can_retry and retry_status and err.status_code in allowed:
                self._sleep(attempt, err.retry_after)
                attempt += 1
                continue
            raise err

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._forget_token()
            if self._owns_client:
                self._client.close()

    # -- internals -------------------------------------------------------------
    def _redact(self, text: str) -> str:
        return redact_secrets(text, self._token.reveal())

    def _authorize(
        self, url: str, query: str, auth: str, headers: dict[str, str]
    ) -> tuple[str, dict[str, str]]:
        """The URL and headers with the token in place."""
        if auth == QUERY:
            token_pair = "token=" + pct(self._token.reveal())
            return f"{url}?{token_pair}&{query}" if query else f"{url}?{token_pair}", headers
        target = f"{url}?{query}" if query else url
        return target, {**headers, "X-Token": self._token.reveal()}

    def _attempt(
        self,
        method: str,
        url: str,
        query: str,
        auth: str,
        headers: dict[str, str],
        content: bytes | None,
        api: str,
        transparent: bool,
        timeout: float,
    ) -> Reply | _Failure:
        """One request. Failures are returned, not raised, so the SdoError the
        caller finally raises has no ``__context__`` pointing at an httpx
        exception whose text or ``.request`` holds the token, and this frame
        (whose locals do) is gone by then."""
        deadline = time.monotonic() + timeout
        target, wire_headers = self._authorize(url, query, auth, headers)
        request: httpx.Request | None = None
        try:
            request = self._client.build_request(method, target, content=content, timeout=timeout)
            request.headers = _wire_headers(request.headers, wire_headers)
            response = self._client.send(request, stream=True, follow_redirects=False)
            try:
                reply = Reply(response.status_code, response.headers.multi_items())
                if is_result(reply, api=api, transparent=transparent):
                    reply.body = self._read(response, deadline, timeout)
                else:
                    reply.body, reply.body_intact = self._read_error_body(response, deadline)
            finally:
                response.close()
        except httpx.TimeoutException as exc:
            return _Failure(
                SdoTimeoutError(
                    self._redact(
                        f"Request timed out (client timeout {timeout:g}s; "
                        f"{type(exc).__name__}: {exc})."
                    )
                ),
                unsent=isinstance(exc, (httpx.ConnectTimeout, httpx.PoolTimeout)),
            )
        except httpx.TransportError as exc:
            return _Failure(
                SdoConnectionError(
                    self._redact(f"Connection failed ({type(exc).__name__}: {exc}).")
                ),
                unsent=isinstance(exc, httpx.ConnectError),
            )
        except _Stop as stop:
            return stop.failure
        except Exception as exc:  # anything else, e.g. from an injected transport
            return _Failure(
                SdoTransportError(self._redact(f"Request failed ({type(exc).__name__}: {exc})."))
            )
        except BaseException as exc:
            # KeyboardInterrupt and the like propagate through this frame: drop
            # the credential-bearing locals so their traceback does not hold them,
            # and clear the finished frames below it (httpx's, an injected
            # transport's), whose locals hold the request and its token URL.
            del target, wire_headers, request
            _clear_frames_below(exc)
            raise
        return reply

    def _read(self, response: httpx.Response, deadline: float, timeout: float) -> bytes:
        """Read a result body and decode it, enforcing the size cap on the raw
        bytes and on every decoding step, and the total deadline per chunk."""
        decoder = BodyDecoder(response.headers.get("content-encoding"), self._max_bytes)
        raw = 0
        try:
            if response.is_stream_consumed:
                # A custom transport (e.g. httpx.MockTransport) returned a
                # response it had already read and decoded: only the cap is left.
                if len(response.content) > self._max_bytes:
                    raise TooLarge
                return response.content
            self._check_deadline(deadline, timeout)  # connect + headers
            for chunk in response.iter_raw():
                raw += len(chunk)
                if raw > self._max_bytes:
                    raise TooLarge
                decoder.feed(chunk)
                self._check_deadline(deadline, timeout)
            return decoder.finish()
        except TooLarge:
            err: SdoError = SdoResponseTooLargeError(
                f"Response exceeded the {self._max_bytes}-byte limit; aborted before "
                "decompressing it fully. Raise max_response_bytes if you expect payloads "
                "this large.",
                limit=self._max_bytes,
            )
        except DecodeError as exc:
            err = SdoTransportError(
                self._redact(f"The response body could not be decompressed ({exc}).")
            )
        raise _Stop(_Failure(err)) from None

    def _read_error_body(self, response: httpx.Response, deadline: float) -> tuple[bytes, bool]:
        """Read up to 64 KiB of an error body: ``(body, intact)``. The status is
        the answer, so a body that is larger, cannot be decompressed, drops or
        times out mid-read yields what was read, never a different error. A
        larger body is intact (its first 64 KiB are what an envelope is parsed
        from); the other failures are not."""
        limit = min(_ERROR_BODY_BYTES, self._max_bytes)
        if response.is_stream_consumed:
            return response.content[:limit], True
        decoder = BodyDecoder(response.headers.get("content-encoding"), limit)
        raw = 0
        try:
            for chunk in response.iter_raw():
                raw += len(chunk)
                decoder.feed(chunk)
                if raw > self._max_bytes:
                    return decoder.partial(), True
                if time.monotonic() > deadline:
                    return decoder.partial(), False
            return decoder.finish(), True
        except TooLarge:
            return decoder.partial(), True
        except Exception:  # DecodeError, a read error or timeout
            return decoder.partial(), False

    def _check_deadline(self, deadline: float, timeout: float) -> None:
        # Each read is also bounded by the same timeout (passed to httpx), so a
        # stalled read cannot overshoot by more than one timeout.
        if time.monotonic() > deadline:
            error = SdoTimeoutError(f"Request exceeded the client's total timeout of {timeout:g}s.")
            raise _Stop(_Failure(error))

    def _sleep(self, attempt: int, retry_after: int | None) -> None:
        if retry_after is not None:
            time.sleep(min(retry_after, _MAX_BACKOFF))
            return
        delay = self._backoff * (2**attempt)
        delay += random.uniform(0, delay * 0.1)  # jitter, proportional so 0 stays 0
        time.sleep(min(delay, _MAX_BACKOFF))


__all__ = [
    "ASYNC",
    "HEADER",
    "QUERY",
    "SYNC",
    "Reply",
    "Transport",
    "build_api_error",
    "effective_timeout",
    "merge_headers",
    "retry_after_seconds",
]
