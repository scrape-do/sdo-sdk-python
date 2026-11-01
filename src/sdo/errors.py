# SPDX-License-Identifier: Apache-2.0
"""The single, normative error taxonomy for the sdo SDK.

The tree is intentionally additive: every concrete error extends a base, so a
caller that writes ``except SdoError`` keeps catching new subclasses added in
future minor releases. Changing an existing error's base class is a breaking
(major) change.

::

    SdoError                     # base — catch-all
    ├── SdoConfigError           # client-side misconfiguration (no HTTP call made)
    ├── SdoTransportError        # no usable HTTP response (network, corrupt body)
    │   ├── SdoConnectionError
    │   ├── SdoTimeoutError      # the client's total timeout elapsed
    │   └── SdoResponseTooLargeError  # response exceeded the size cap
    └── SdoAPIError              # an error status (also: any other 4xx)
        ├── SdoAuthError         # 401 / 403  — bad token, or credit exhausted
        ├── SdoValidationError   # 400        — the request was rejected
        ├── SdoConcurrencyError  # 429        — concurrent-request limit hit
        └── SdoGatewayError      # 5xx        — gateway-origin upstream failure

Every failure the SDK can produce is one of these; no ``httpx`` or JSON
exception escapes. Messages, retained bodies and causes are credential-masked.

Note: a *successful* scrape of a page that itself returned a 404/410 is NOT an
error — it is a ``ScrapeResult`` whose ``status_code`` carries the target's
status. With ``transparent_response=True``, any status the target itself
returned (marked by ``Scrape.do-Initial-Status-Code``) is a result as well.
"""

from __future__ import annotations

import re

from ._wire import pct

#: What a credential is replaced with.
_MASK = "***"
#: The literal token is only scrubbed when it is at least this long, so a short
#: test token cannot shred ordinary text.
_LITERAL_MIN_LENGTH = 8

#: Credential-shaped values from any source, matched by shape so text the SDK
#: never built (a proxy message, an echoed request) is covered too. ASCII word
#: boundaries: "mytoken=" is not a credential.
_SHAPES = (
    (
        re.compile(r"\b((?:token|api[-_]?key|access[-_]?token)=)[^&\s\"']+", re.I | re.A),
        r"\1" + _MASK,
    ),
    (
        re.compile(r"(\"(?:token|api[-_]?key|access[-_]?token)\"\s*:\s*\")[^\"]*(\")", re.I | re.A),
        r"\1" + _MASK + r"\2",
    ),
    (re.compile(r"\b(x-token\s*:\s*)[^\s,;]+", re.I | re.A), r"\1" + _MASK),
)


def redact_secrets(text: str, token: str | None = None) -> str:
    """Mask credentials in ``text`` before it can reach an error, a log or a
    retained body.

    With ``token`` (at least 8 characters), that exact value and its
    percent-encoded form are masked wherever they appear; credential-shaped
    values (``token=…``, ``"api_key": "…"``, ``X-Token: …``) are masked
    whatever their source.
    """
    if token and len(token) >= _LITERAL_MIN_LENGTH:
        text = text.replace(token, _MASK)
        try:
            encoded = pct(token)
        except UnicodeEncodeError:  # a lone surrogate never reaches the wire anyway
            encoded = token
        if encoded != token:
            text = text.replace(encoded, _MASK)
    for pattern, repl in _SHAPES:
        text = pattern.sub(repl, text)
    return text


class SdoError(Exception):
    """Base class for every error raised by the SDK."""

    #: The human-readable message (credentials already masked).
    message: str

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message


class SdoConfigError(SdoError):
    """The client was used incorrectly (e.g. missing token, mutually exclusive
    options). No HTTP request is made."""


class SdoTransportError(SdoError):
    """The request did not produce a usable HTTP response: DNS, TCP, TLS and read
    failures, a corrupt compressed body, or a success body that is not the JSON
    the API promised."""


class SdoConnectionError(SdoTransportError):
    """The connection could not be established."""


class SdoTimeoutError(SdoTransportError):
    """The request exceeded the client timeout that applied to it (named in the
    message). Only a connect timeout is retried; a request that was sent is
    not, since the gateway may still run and bill it."""


class SdoResponseTooLargeError(SdoTransportError):
    """The response exceeded the client's size ceiling.

    Transparent decompression is an attack surface: the scraped target controls
    the body, so a small compressed payload can inflate without bound. Reading
    and inflating are both capped and abort as soon as the ceiling is crossed,
    before the full buffer is materialized.
    """

    #: The byte ceiling that was exceeded.
    limit: int

    def __init__(self, message: str, *, limit: int) -> None:
        super().__init__(message)
        self.limit = limit


class SdoAPIError(SdoError):
    """The API returned an error status.

    Carries the HTTP ``status_code``, whether the request was ``billed`` (by
    ``Scrape.do-Request-Cost``, else the documented status table), its ``cost``,
    ``is_retryable`` and ``retry_after`` so callers and the retry policy can act
    uniformly across languages.

    This base class is raised as-is for a status the taxonomy has no narrower
    class for (e.g. 405, 409, or a redirect from the async job API).
    """

    #: The HTTP status code the API returned.
    status_code: int
    #: True when this request consumed credits: ``Scrape.do-Request-Cost`` when
    #: readable (0 is not billed), else the documented status table (the
    #: gateway's own 400, 401, 429, 502 and 510 are not billed). The async job
    #: API never bills an error.
    billed: bool
    #: ``Scrape.do-Request-Cost``, read like ``ScrapeResult.cost`` (``None`` when
    #: absent or unreadable, and for the job API). An error's cost is 0: a
    #: positive cost makes the response a result.
    cost: int | None
    #: True when retrying the *same* request may succeed.
    is_retryable: bool
    #: Whole seconds to wait before retrying, from ``Retry-After`` when present.
    retry_after: int | None
    #: The error body with credentials masked, truncated to 2048 characters.
    body: str | None

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        billed: bool = False,
        is_retryable: bool = False,
        retry_after: int | None = None,
        body: str | None = None,
        cost: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.billed = billed
        self.cost = cost
        self.is_retryable = is_retryable
        self.retry_after = retry_after
        self.body = body


class SdoAuthError(SdoAPIError):
    """401/403 — the token is missing, invalid, not permitted, or the account's
    monthly credit balance is exhausted.

    The gateway has no separate 402 tier: a depleted balance also returns 401, so
    the two cases are told apart by the error message, not the status code.
    """


class SdoValidationError(SdoAPIError):
    """400 — the gateway rejected the request parameters."""


class SdoConcurrencyError(SdoAPIError):
    """429 — the account's concurrent-request limit was reached."""

    #: The ``Scrape.do-Remaining-Credits`` header value, when present.
    remaining_credits: int | None

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        billed: bool = False,
        is_retryable: bool = False,
        retry_after: int | None = None,
        body: str | None = None,
        remaining_credits: int | None = None,
        cost: int | None = None,
    ) -> None:
        super().__init__(
            message,
            status_code=status_code,
            billed=billed,
            is_retryable=is_retryable,
            retry_after=retry_after,
            body=body,
            cost=cost,
        )
        self.remaining_credits = remaining_credits


class SdoGatewayError(SdoAPIError):
    """A gateway-origin 5xx (e.g. 502/503) — the upstream fetch failed inside
    Scrape.do. ``is_retryable`` tells which ones are worth retrying. A 510 is
    the gateway's record of a request its HTTP client cancelled before it
    finished: not an upstream failure, and not retryable."""


__all__ = [
    "redact_secrets",
    "SdoError",
    "SdoConfigError",
    "SdoTransportError",
    "SdoConnectionError",
    "SdoTimeoutError",
    "SdoResponseTooLargeError",
    "SdoAPIError",
    "SdoAuthError",
    "SdoValidationError",
    "SdoConcurrencyError",
    "SdoGatewayError",
]
