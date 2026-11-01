# SPDX-License-Identifier: Apache-2.0
"""The synchronous ``Client`` — the SDK's front door.

Token is the sole auth, resolved with explicit precedence:
``Client(token=...)`` argument **>** ``SDO_TOKEN`` env var. Every request sends
``Accept-Encoding: gzip, deflate, br`` and the response is transparently
decompressed. No telemetry: the only client-identifying header is the
User-Agent (``sdo-sdk-python/<version>``).

Environment (read only when the matching argument is not given):
``SDO_TOKEN``, ``SDO_BASE_URL``, ``SDO_ASYNC_BASE_URL``, ``SDO_TIMEOUT``
(seconds).
"""

from __future__ import annotations

import math
import os
import re
from collections.abc import Mapping
from typing import Any

import httpx

from . import _urls
from ._secret import Secret, resolve_token
from ._transport import Transport, effective_timeout, merge_headers
from ._version import __version__
from ._wire import jdump, query
from .config import ScrapeConfig
from .errors import SdoConfigError
from .jobs import AsyncJobs
from .response import ScrapeResult
from .usage import Usage, parse_usage

_DEFAULT_BASE_URL = "https://api.scrape.do"
_DEFAULT_ASYNC_BASE_URL = "https://q.scrape.do"
#: Total per-attempt timeout: the gateway's default ``timeout`` (60 s) plus the
#: 10 s margin, so the gateway's answer to a request that sets no ``timeout``
#: always arrives first. A request whose gateway ``timeout`` is longer gets a
#: longer attempt (see ``effective_timeout``).
_DEFAULT_TIMEOUT = 70.0
_DEFAULT_ACCEPT_ENCODING = "gzip, deflate, br"
#: Ceiling for a response body, raw and decompressed. The scraped target
#: controls the payload, so transparent decompression is bounded by default.
_MAX_RESPONSE_BYTES = 32 * 1024 * 1024
#: Plain http is only allowed to these hosts (unless explicitly opted in).
_LOOPBACK = ("localhost", "127.0.0.1", "::1")
_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
#: SDO_TIMEOUT: a plain decimal number of seconds, at least a millisecond.
_TIMEOUT_RE = re.compile(r"[0-9]+(?:\.[0-9]+)?", re.ASCII)
_MIN_ENV_TIMEOUT = 0.001


def _user_agent() -> str:
    """The only client-identifying header, and deliberately the whole of it.

    No OS and no interpreter version: the spec fixes the User-Agent at
    ``sdo-sdk-<lang>/<version>``, and anything appended is extra client
    fingerprinting the no-telemetry rule forbids.
    """
    return f"sdo-sdk-python/{__version__}"


def check_base_url(url: str, *, name: str, allow_insecure_http: bool = False) -> str:
    """A base URL is scheme + host (+ port) + an optional path prefix, which is
    kept. The token only travels over HTTPS: ``https`` is always accepted, plain
    ``http`` only for a loopback host or with ``allow_insecure_http=True``.
    Userinfo, a query or a fragment (even an empty ``?``) is rejected."""
    parts = _urls.split(url)
    if parts is None or parts.userinfo is not None or "?" in url or "#" in url:
        raise SdoConfigError(
            f"{name} must be an absolute https:// URL (scheme, host, optional port and "
            f"path prefix; no credentials, query or fragment), got {_urls.shown(url)}."
        )
    loopback = parts.host.lower() in _LOOPBACK
    if parts.scheme == "https" or allow_insecure_http or loopback:
        return url.rstrip("/")
    raise SdoConfigError(
        f"{name} must use https:// (plain http is only allowed for localhost, 127.0.0.1 "
        f"and ::1, or with allow_insecure_http=True), got {_urls.shown(url)}."
    )


def _env_timeout(raw: str) -> float:
    seconds = float(raw) if _TIMEOUT_RE.fullmatch(raw) else math.nan
    if not seconds >= _MIN_ENV_TIMEOUT:  # also false for NaN
        raise SdoConfigError(
            "SDO_TIMEOUT must be a decimal number of seconds of at least 0.001 "
            f"(e.g. 30 or 1.5), got {raw!r}."
        )
    return seconds


class Client:
    """Synchronous Scrape.do client.

    :param token: API token. Surrounding whitespace is trimmed; if omitted
        (or blank), ``SDO_TOKEN`` is read. It must be visible ASCII.
    :param base_url: sync API base (default ``https://api.scrape.do``; env
        ``SDO_BASE_URL``): scheme, host, optional port and an optional path
        prefix, which is kept. Must be ``https://`` unless the host is loopback.
    :param async_base_url: async job API base (default ``https://q.scrape.do``;
        env ``SDO_ASYNC_BASE_URL``). Same rules.
    :param timeout: total wall-clock timeout per request attempt, in
        **seconds**, over connect, headers and the whole body (default 70: the
        gateway's 60-second default plus 10; env ``SDO_TIMEOUT``, a decimal
        number of at least 0.001). httpx can only
        enforce it between reads (each read is bounded by the same timeout), so
        a peer that trickles bytes can stretch one attempt to about twice the
        timeout. When a scrape sets the gateway-side ``ScrapeConfig.timeout``
        (milliseconds), or a plugin call a positive integer ``timeout`` param,
        that attempt waits at least that plus 10 seconds.
    :param retries: client-side retries (default 2; the gateway already retries
        server-side, so keep this low). Retried: the retryable statuses (429,
        502, 503, 504, 520-524; not 510, the gateway's record of a request the
        client cancelled) and failures before the request was sent
        (DNS, connect, TLS, connect timeout). Never retried: a failure after
        the request was sent (read timeout, dropped connection, a body that
        cannot be decoded or is too large), since the gateway may still run and
        bill it. Requests that can change the target (POST/PUT/PATCH/DELETE)
        are retried only on 429.
    :param backoff_factor: base of the exponential backoff, in seconds.
    :param accept_encoding: override the advertised ``Accept-Encoding``.
    :param prefer_brotli: advertise ``Accept-Encoding: br`` alone so the gateway
        actually returns brotli (it picks the first of gzip/deflate/br, so the
        default ``gzip, deflate, br`` yields gzip in practice).
    :param max_response_bytes: ceiling for a response body, raw and decompressed
        (default 32 MiB). Exceeding it raises ``SdoResponseTooLargeError``
        instead of inflating the whole payload.
    :param http_client: an ``httpx.Client`` to send through (proxies, mTLS,
        tests). The SDK still sends absolute URLs to its own base URLs, applies
        its own timeout and headers (the client's default headers are not sent)
        and never follows redirects. It is not closed by :meth:`close`.
        Without one, the SDK's own client ignores the environment
        (``trust_env=False``: no proxy, CA or ``.netrc`` variables).
    :param allow_insecure_http: allow a plain ``http://`` base URL on a
        non-loopback host. The token is then sent in cleartext.
    """

    def __init__(
        self,
        token: str | None = None,
        *,
        base_url: str | None = None,
        async_base_url: str | None = None,
        timeout: float | None = None,
        retries: int = 2,
        backoff_factor: float = 0.5,
        accept_encoding: str | None = None,
        prefer_brotli: bool = False,
        max_response_bytes: int = _MAX_RESPONSE_BYTES,
        http_client: httpx.Client | None = None,
        allow_insecure_http: bool = False,
    ) -> None:
        secret = Secret(token)
        del token  # no frame keeps the raw value (error trackers record locals)
        self._configure(
            secret,
            base_url=base_url,
            async_base_url=async_base_url,
            timeout=timeout,
            retries=retries,
            backoff_factor=backoff_factor,
            accept_encoding=accept_encoding,
            prefer_brotli=prefer_brotli,
            max_response_bytes=max_response_bytes,
            http_client=http_client,
            allow_insecure_http=allow_insecure_http,
        )

    @classmethod
    def _from_secret(cls, secret: Secret, **options: Any) -> Client:
        self = cls.__new__(cls)
        self._configure(secret, **options)
        return self

    def _configure(
        self,
        secret: Secret,
        *,
        base_url: str | None = None,
        async_base_url: str | None = None,
        timeout: float | None = None,
        retries: int = 2,
        backoff_factor: float = 0.5,
        accept_encoding: str | None = None,
        prefer_brotli: bool = False,
        max_response_bytes: int = _MAX_RESPONSE_BYTES,
        http_client: httpx.Client | None = None,
        allow_insecure_http: bool = False,
    ) -> None:
        resolved = resolve_token(secret)
        if isinstance(resolved, str):
            raise SdoConfigError(resolved)

        self.base_url = check_base_url(
            base_url or os.environ.get("SDO_BASE_URL") or _DEFAULT_BASE_URL,
            name="base_url",
            allow_insecure_http=allow_insecure_http,
        )
        self.async_base_url = check_base_url(
            async_base_url or os.environ.get("SDO_ASYNC_BASE_URL") or _DEFAULT_ASYNC_BASE_URL,
            name="async_base_url",
            allow_insecure_http=allow_insecure_http,
        )

        if timeout is None:
            env_t = os.environ.get("SDO_TIMEOUT")
            timeout = _env_timeout(env_t) if env_t else _DEFAULT_TIMEOUT
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not (math.isfinite(timeout) and timeout > 0)
        ):
            raise SdoConfigError(f"timeout must be a positive number of seconds, got {timeout!r}.")
        if isinstance(retries, bool) or not isinstance(retries, int):
            raise SdoConfigError(f"retries must be an int, got {retries!r}.")
        if (
            isinstance(backoff_factor, bool)
            or not isinstance(backoff_factor, (int, float))
            or not (math.isfinite(backoff_factor) and backoff_factor >= 0)
        ):
            raise SdoConfigError(f"backoff_factor must be >= 0, got {backoff_factor!r}.")
        if (
            isinstance(max_response_bytes, bool)
            or not isinstance(max_response_bytes, int)
            or (max_response_bytes <= 0)
        ):
            raise SdoConfigError(
                f"max_response_bytes must be a positive int, got {max_response_bytes!r}."
            )
        if prefer_brotli:
            accept_encoding = "br"
        merge_headers({"Accept-Encoding": accept_encoding or _DEFAULT_ACCEPT_ENCODING})

        self._timeout = float(timeout)
        self._transport = Transport(
            token=resolved,
            timeout=self._timeout,
            accept_encoding=accept_encoding or _DEFAULT_ACCEPT_ENCODING,
            retries=retries,
            backoff_factor=float(backoff_factor),
            user_agent=_user_agent(),
            max_response_bytes=max_response_bytes,
            http_client=http_client,
        )
        self._jobs: AsyncJobs | None = None

    def __repr__(self) -> str:
        # never the token; getattr: a half-built client (its constructor
        # raised) may be repr'd by a traceback that records locals
        return f"Client(base_url={getattr(self, 'base_url', None)!r})"

    # -- scraping ------------------------------------------------------------
    def scrape(
        self,
        config: ScrapeConfig | str | None = None,
        *,
        url: str | None = None,
        method: str | None = None,
        data: bytes | str | None = None,
        json: Any | None = None,
        headers: Mapping[str, str] | None = None,
        **config_kwargs: Any,
    ) -> ScrapeResult:
        """Scrape a URL and return a :class:`ScrapeResult`.

        Accepts either a :class:`ScrapeConfig`, or a URL string plus keyword
        options::

            client.scrape("https://example.com", render=True, geo_code="us")
            client.scrape(ScrapeConfig(url="https://example.com", super_proxy=True))

        ``method`` defaults to GET, or POST when a body is given. ``json`` is
        sent as canonical JSON with ``Content-Type: application/json`` (unless
        ``headers`` sets one); ``data`` (str or bytes) is sent as-is.

        ``headers`` (and ``ScrapeConfig.headers``) are compared
        case-insensitively; a caller value replaces the SDK's own
        ``User-Agent`` or ``Accept-Encoding``. Names must be HTTP tokens and
        values printable ASCII; ``Host``, ``Content-Length``,
        ``Transfer-Encoding`` and ``Connection`` cannot be set.

        When the config sets the gateway ``timeout`` (milliseconds), this
        request's per-attempt client timeout is at least that plus 10 seconds.
        """
        cfg = self._coerce_config(config, url, config_kwargs)
        params = cfg.to_params()
        qs = _query(params)
        # Call headers replace config headers of the same name, in any case.
        merged = merge_headers(cfg.headers, headers)
        http_method, content, req_headers = _request_parts(method, data, json, merged)
        # The gateway renders GET requests only and answers any other method
        # with a 400.
        render = dict(params).get("render")
        if render not in (None, "false") and http_method != "GET":
            raise SdoConfigError(f"render works with GET requests only; got {http_method}.")
        reply = self._transport.send(
            http_method,
            f"{self.base_url}/",
            query=qs,
            headers=req_headers,
            content=content,
            transparent=cfg.transparent_response is True,
            timeout=effective_timeout(self._timeout, cfg.timeout),
        )
        return ScrapeResult(
            content=reply.body, status_code=reply.status_code, headers=reply.headers, url=cfg.url
        )

    @staticmethod
    def _coerce_config(
        config: ScrapeConfig | str | None,
        url: str | None,
        config_kwargs: dict[str, Any],
    ) -> ScrapeConfig:
        if isinstance(config, ScrapeConfig):
            if config_kwargs or url:
                raise SdoConfigError("Pass a ScrapeConfig OR url+kwargs, not both.")
            return config
        target = config if isinstance(config, str) else url
        if not target:
            raise SdoConfigError("scrape() needs a url (string or ScrapeConfig).")
        try:
            return ScrapeConfig(url=target, **config_kwargs)
        except TypeError as exc:  # an unknown keyword
            raise SdoConfigError(f"Invalid scrape() option: {exc}") from None

    # -- plugins (generic core) ---------------------------------------------
    def plugin(self, route: str, params: Mapping[str, Any] | None = None, **kwargs: Any) -> Any:
        """Call a documented plugin endpoint generically and return its JSON
        (or the raw bytes when the response is not JSON).

        ``route`` is the path under ``/plugin/`` (e.g. ``"google/shopping"``);
        ``"amazon"`` (or ``"amazon/"``) is the Amazon raw-HTML plugin at
        ``/plugin/amazon/``. New plugins are usable the day they ship — no SDK
        release required::

            client.plugin("google/shopping", {"q": "laptop", "gl": "us"})

        :meth:`plugin_result` returns the same response with its status and
        headers.

        Parameters are sent sorted by key; a list repeats its key, a bool is
        ``true``/``false``, a dict is canonical JSON, ``None`` is skipped. A
        nested list, an empty key or the key ``token`` (any case) is a
        :class:`~sdo.SdoConfigError`.
        """
        from .plugins import call_plugin

        if params is not None and not isinstance(params, Mapping):
            raise SdoConfigError(
                f"Plugin parameters must be a mapping, got {type(params).__name__}."
            )
        merged: dict[str, Any] = dict(params or {})
        merged.update(kwargs)
        return call_plugin(self._transport, self.base_url, route, merged)

    def plugin_result(
        self, route: str, params: Mapping[str, Any] | None = None, **kwargs: Any
    ) -> ScrapeResult:
        """Call a plugin like :meth:`plugin`, but return the delivered response
        as a :class:`ScrapeResult`: its ``status_code``, headers (``cost``,
        ``remaining_credits``) and body (``.json()`` parses
        it). ``url`` is ``""``."""
        from .plugins import plugin_reply

        if params is not None and not isinstance(params, Mapping):
            raise SdoConfigError(
                f"Plugin parameters must be a mapping, got {type(params).__name__}."
            )
        merged: dict[str, Any] = dict(params or {})
        merged.update(kwargs)
        reply = plugin_reply(self._transport, self.base_url, route, merged)
        return ScrapeResult(
            content=reply.body, status_code=reply.status_code, headers=reply.headers, url=""
        )

    # -- account -------------------------------------------------------------
    def usage(self) -> Usage:
        """The account's usage statistics (``GET <base_url>/info``, the token
        as a query parameter); costs no credits.

        The endpoint allows 10 requests per minute per IP address and answers
        429 above that, so an error status is not retried (a quick retry only
        spends more of the same minute); a failure before the request was sent
        still is. An unknown token is answered with a 502 "authentication
        refused". A body without the documented fields raises
        :class:`~sdo.SdoTransportError`."""
        reply = self._transport.send("GET", f"{self.base_url}/info", retry_status=False)
        return parse_usage(reply.body)

    # -- async jobs ----------------------------------------------------------
    @property
    def jobs(self) -> AsyncJobs:
        """The async (``q.scrape.do``) REST job API. See :mod:`sdo.jobs`."""
        if self._jobs is None:
            self._jobs = AsyncJobs._bound(self._transport, base_url=self.async_base_url)
        return self._jobs

    # -- lifecycle -----------------------------------------------------------
    def close(self) -> None:
        self._transport.close()

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _query(pairs: list[tuple[str, str]]) -> str:
    try:
        return query(pairs)
    except UnicodeEncodeError:  # a lone surrogate cannot be UTF-8 encoded
        raise SdoConfigError("Request parameters must be valid Unicode text.") from None


def _request_parts(
    method: str | None,
    data: bytes | str | None,
    json_body: Any | None,
    headers: Mapping[str, str],
) -> tuple[str, bytes | None, dict[str, str]]:
    """Validate the HTTP method and body of a scrape and build them."""
    req_headers = dict(headers)
    if json_body is not None and data is not None:
        raise SdoConfigError("Pass json= or data=, not both.")
    content: bytes | None = None
    if json_body is not None:
        try:
            content = jdump(json_body).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise SdoConfigError(f"json= is not JSON-serializable: {exc}") from None
        if not any(k.lower() == "content-type" for k in req_headers):
            req_headers["Content-Type"] = "application/json"
    elif data is not None:
        if isinstance(data, str):
            try:
                content = data.encode("utf-8")
            except UnicodeEncodeError:  # a lone surrogate
                raise SdoConfigError("data= must be valid Unicode text.") from None
        elif isinstance(data, (bytes, bytearray)):
            content = bytes(data)
        else:
            raise SdoConfigError(f"data= must be str or bytes, got {type(data).__name__}.")
    if method is None:
        http_method = "GET" if content is None else "POST"
    elif isinstance(method, str) and method.upper() in _METHODS:
        http_method = method.upper()
    else:
        raise SdoConfigError(f"method must be one of {_METHODS}, got {method!r}.")
    if content is not None and http_method in ("GET", "HEAD"):
        raise SdoConfigError(f"A {http_method} request cannot carry a body.")
    return http_method, content, req_headers


__all__ = ["Client"]
