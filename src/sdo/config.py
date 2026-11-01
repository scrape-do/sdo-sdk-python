# SPDX-License-Identifier: Apache-2.0
"""``ScrapeConfig`` — the single, serializable request spec.

Every public scrape parameter is a field here. The object is passed to
``Client.scrape(...)``, so it is inspectable, reusable, and serializable. Field
names are Pythonic snake_case; each maps to its canonical wire name (the
gateway matches query keys case-insensitively).

The documented gateway parameters are modelled; ``extra_params`` sends any
other key (an ASCII letter, then letters, digits, ``_``, ``.`` or ``-``)
verbatim.

Serialization rules (identical in every SDK):

* an unset value is omitted and never validated: ``None``, ``False``, ``0``,
  ``""`` and an empty list or dict all mean "unset" — except for the tri-state
  ``block_resources``, where only ``None`` is unset and ``False`` is sent;
* enum values are validated case-insensitively and sent lowercase;
* the millisecond ranges from the public docs are enforced, and so are the
  documented dependencies between parameters (see ``_check_dependencies``);
* typed parameters go out in a fixed order, then ``extra_params`` sorted by key.
"""

from __future__ import annotations

import math
import re
from dataclasses import KW_ONLY, dataclass
from typing import Any

from . import _urls
from ._wire import es_number, jdump
from .errors import SdoConfigError

# Parameters that only take effect when render is enabled. Setting any of these
# without render is almost always a mistake, so it is rejected client-side for
# fast feedback (the gateway remains authoritative).
_RENDER_ONLY = (
    "custom_wait",
    "wait_selector",
    "wait_until",
    "block_resources",
    "play_with_browser",
    "show_websocket_requests",
    "show_frames",
    "screenshot",
    "full_screenshot",
    "particular_screenshot",
    "height",
    "width",
)

#: The documented sessionId range; at most 7 digits is the gateway's own limit.
_MAX_SESSION_ID = 1_000_000
_SESSION_ID_RE = re.compile(r"[0-9]{1,7}", re.ASCII)
#: Options that put their output in the JSON response, so need ``return_json``.
_NEEDS_RETURN_JSON = (
    "show_websocket_requests",
    "show_frames",
    "screenshot",
    "full_screenshot",
    "particular_screenshot",
)

#: Parameters with a declared value set: validated case-insensitively, sent lowercase.
_ENUMS = {
    "render": ("true", "false"),
    "device": ("desktop", "mobile", "tablet"),
    "wait_until": ("domcontentloaded", "load", "networkidle0", "networkidle2"),
    "output": ("raw", "markdown"),
}

#: Documented ranges, in milliseconds.
_RANGES = {
    "timeout": (5000, 120000),
    "retry_timeout": (5000, 55000),
    "custom_wait": (0, 35000),
}

# (field, wire key, kind) in the canonical wire order. ``render`` has its own
# rules and is handled inline.
# ``_TRIBOOL`` is a bool whose ``False`` is sent too (``None`` is unset).
_BOOL, _INT, _STR, _ENUM, _JSON_ARRAY, _TRIBOOL = range(6)
_PARAMS: tuple[tuple[str, str, int], ...] = (
    ("super_proxy", "super", _BOOL),
    ("device", "device", _ENUM),
    ("timeout", "timeout", _INT),
    ("retry_timeout", "retryTimeout", _INT),
    ("disable_retry", "disableRetry", _BOOL),
    ("geo_code", "geoCode", _STR),
    ("regional_geo_code", "regionalGeoCode", _STR),
    ("zip_code", "zipCode", _STR),
    ("session_id", "sessionId", _STR),
    ("set_cookies", "setCookies", _STR),
    ("custom_headers", "customHeaders", _BOOL),
    ("extra_headers", "extraHeaders", _BOOL),
    ("forward_headers", "forwardHeaders", _BOOL),
    ("custom_wait", "customWait", _INT),
    ("wait_selector", "waitSelector", _STR),
    ("wait_until", "waitUntil", _ENUM),
    ("block_resources", "blockResources", _TRIBOOL),
    ("play_with_browser", "playWithBrowser", _JSON_ARRAY),
    ("show_websocket_requests", "showWebsocketRequests", _BOOL),
    ("show_frames", "showFrames", _BOOL),
    ("screenshot", "screenShot", _BOOL),
    ("full_screenshot", "fullScreenShot", _BOOL),
    ("particular_screenshot", "particularScreenShot", _STR),
    ("height", "height", _INT),
    ("width", "width", _INT),
    ("output", "output", _ENUM),
    ("return_json", "returnJSON", _BOOL),
    ("pure_cookies", "pureCookies", _BOOL),
    ("disable_redirection", "disableRedirection", _BOOL),
    ("transparent_response", "transparentResponse", _BOOL),
)

#: Keys ``extra_params`` may not set (compared case-insensitively): the token
#: must only come from the client, and the target only from ``url``.
_RESERVED_EXTRA = ("token", "url")
#: The shape of an ``extra_params`` key: an ASCII letter, then letters, digits,
#: ``_``, ``.`` or ``-``. Every API parameter fits; anything else (``&``, ``=``,
#: ``[``, whitespace, a look-alike of ``token``) is a typo or an injection.
_PARAM_KEY_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.\-]*", re.ASCII)


def _is_unset(value: Any) -> bool:
    return value is None or value is False or value == 0 or value in ("", [], {}, ())


def validate_target_url(url: Any) -> str:
    """The scrape target: ``http`` or ``https`` (any case), ``://``, a non-empty
    host with an optional ``userinfo@`` and port 1-65535, and no whitespace,
    control character or backslash anywhere. It is otherwise sent exactly as
    given; the gateway is the real SSRF boundary."""
    if not isinstance(url, str) or not url:
        raise SdoConfigError("ScrapeConfig requires a non-empty 'url' string.")
    if _urls.split(url) is None:
        raise SdoConfigError(
            f"url must be an absolute http:// or https:// URL with a host, got {_urls.shown(url)}."
        )
    return url


def _extra_value(key: str, value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise SdoConfigError(f"extra_params[{key!r}] must be a finite number.")
        return es_number(value)
    if isinstance(value, str):
        return value
    raise SdoConfigError(
        f"extra_params[{key!r}] must be a str, bool, int or float, got {type(value).__name__}."
    )


@dataclass
class ScrapeConfig:
    """A reusable, serializable scrape request specification.

    ``url`` may be given positionally; every other field is keyword-only, so
    new fields can be added in any order without breaking callers.
    """

    url: str
    _: KW_ONLY

    # rendering / proxy
    render: bool | str | None = None  # True | "true" | "false"
    super_proxy: bool | None = None  # wire: super
    device: str | None = None  # desktop | mobile | tablet

    # timeouts (gateway-side, MILLISECONDS)
    #: Total gateway request timeout in milliseconds (5000-120000). Not the
    #: client's HTTP timeout, which is ``Client(timeout=...)`` in seconds; when
    #: set, that request's client timeout is at least this plus 10 seconds.
    timeout: int | None = None
    #: Per-attempt gateway retry timeout in milliseconds (5000-55000). Cannot be
    #: combined with render.
    retry_timeout: int | None = None
    disable_retry: bool | None = None

    # geo targeting
    geo_code: str | None = None
    #: A region (europe, asia, africa, oceania, northamerica, southamerica).
    #: Requires ``super_proxy``; cannot be combined with ``geo_code``.
    regional_geo_code: str | None = None
    #: Postal/ZIP code targeting. Requires ``super_proxy`` and ``geo_code``.
    zip_code: str | None = None

    # session / cookies / headers
    #: Sticky proxy session (the same IP across requests): an integer
    #: 0-1000000, as an int or a str of decimal digits (at most 7). ``0`` is a
    #: valid id and is sent; ``None`` and ``""`` are unset.
    session_id: int | str | None = None
    #: Cookies to pre-set, as the RAW string ``name=value; name2=value2``. The
    #: gateway expects this URL-encoded and the SDK encodes it as a query value,
    #: so pre-encoding it yourself double-encodes and breaks it.
    set_cookies: str | None = None
    #: ``custom_headers``, ``extra_headers`` and ``forward_headers`` are
    #: mutually exclusive.
    custom_headers: bool | None = None
    extra_headers: bool | None = None
    forward_headers: bool | None = None
    #: Actual headers to forward to the target (sent as request headers; pairs
    #: with one of the custom/extra/forward header flags).
    headers: dict[str, str] | None = None

    # rendering behaviour (render-only)
    #: Extra wait after page load, in milliseconds (0-35000).
    custom_wait: int | None = None
    wait_selector: str | None = None
    wait_until: str | None = None
    #: Tri-state. ``None`` sends nothing and the gateway's default, ``True``
    #: (resources blocked), applies. ``True`` sends ``blockResources=true`` and
    #: needs render; ``False`` sends ``blockResources=false`` to load resources,
    #: and does not need render.
    block_resources: bool | None = None
    #: A list of browser action objects, serialized to canonical JSON.
    play_with_browser: list[dict[str, Any]] | None = None
    #: WebSocket messages in the JSON response (needs render and return_json).
    show_websocket_requests: bool | None = None
    #: iframe content in the JSON response (needs render and return_json).
    show_frames: bool | None = None

    # screenshots (render-only, and each needs return_json: the image comes
    # back base64-encoded in the JSON response). Only one may be set, and
    # particular_screenshot cannot be combined with play_with_browser.
    screenshot: bool | None = None
    full_screenshot: bool | None = None
    particular_screenshot: str | None = None  # CSS selector
    #: Viewport height and width in px (render-only).
    height: int | None = None
    width: int | None = None

    # output / behaviour
    output: str | None = None  # raw | markdown
    #: Return the network requests and page content as JSON. With render it
    #: carries the browser's XHR/fetch requests; without render the gateway
    #: wraps the plain response in the same JSON envelope.
    return_json: bool | None = None
    #: Return the target's original ``Set-Cookie`` header instead of
    #: ``Scrape.do-Cookies``; the body is unchanged.
    pure_cookies: bool | None = None
    disable_redirection: bool | None = None
    transparent_response: bool | None = None

    #: Escape hatch for any parameter not modelled above, sent verbatim after
    #: the typed ones (sorted by key). Values may be str, bool (``"true"`` /
    #: ``"false"``), int or float; ``None`` skips the key.
    extra_params: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        validate_target_url(self.url)

    # -- validation ------------------------------------------------------------
    def _render_value(self) -> str | None:
        """The normalized render value that goes on the wire, or ``None``."""
        r = self.render
        if _is_unset(r):
            return None
        if r is True:
            return "true"
        if not isinstance(r, str):
            raise SdoConfigError(f"render must be a bool or one of {_ENUMS['render']}, got {r!r}.")
        return self._enum("render", r)

    @staticmethod
    def _enum(name: str, value: Any) -> str:
        allowed = _ENUMS[name]
        if not isinstance(value, str) or value.lower() not in allowed:
            raise SdoConfigError(f"{name} must be one of {allowed}, got {value!r}.")
        return value.lower()

    @staticmethod
    def _check_int(name: str, value: Any) -> int:
        unit = " (milliseconds)" if name in _RANGES else ""
        if isinstance(value, bool) or not isinstance(value, int):
            raise SdoConfigError(f"{name} must be an integer{unit}, got {value!r}.")
        if name in _RANGES:
            lo, hi = _RANGES[name]
            if not lo <= value <= hi:
                # scrape(url, timeout=30) reads like a requests-style timeout in
                # seconds; say so instead of sending a 30 ms budget.
                hint = (
                    " The client's HTTP timeout is Client(timeout=...), in seconds."
                    if name == "timeout"
                    else ""
                )
                raise SdoConfigError(
                    f"{name} is in milliseconds and must be between {lo} and {hi}, "
                    f"got {value}.{hint}"
                )
        return value

    def validate(self) -> None:
        """Client-side validation; the gateway stays authoritative. Raises
        :class:`SdoConfigError`. Called by :meth:`to_params`."""
        self.to_params()

    # -- serialization ---------------------------------------------------------
    def to_params(self) -> list[tuple[str, str]]:
        """Serialize to the ordered ``(wire_key, value)`` query pairs.

        Raises :class:`SdoConfigError` for an invalid configuration.
        """
        validate_target_url(self.url)
        render = self._render_value()
        render_on = render == "true"

        out: list[tuple[str, str]] = [("url", self.url)]
        if render is not None and render_on:
            out.append(("render", render))

        for name, wire, kind in _PARAMS:
            value = getattr(self, name)
            if name == "session_id":
                # An id, not a quantity: 0 is sent; only None and "" are unset.
                if value is not None and value != "":
                    out.append((wire, self._session_id(value)))
                continue
            if kind == _TRIBOOL:
                if value is None:
                    continue
                if not isinstance(value, bool):
                    raise SdoConfigError(f"{name} must be a bool, got {value!r}.")
                # False turns the render-only feature off, so it needs no render.
                if value and name in _RENDER_ONLY and not render_on:
                    raise SdoConfigError(
                        f"'{name}' requires render to be enabled (set render=True)."
                    )
                out.append((wire, "true" if value else "false"))
                continue
            if _is_unset(value):
                continue
            if name in _RENDER_ONLY and not render_on:
                raise SdoConfigError(f"'{name}' requires render to be enabled (set render=True).")
            if kind == _BOOL:
                if value is not True:
                    raise SdoConfigError(f"{name} must be a bool, got {value!r}.")
                out.append((wire, "true"))
            elif kind == _INT:
                out.append((wire, str(self._check_int(name, value))))
            elif kind == _STR:
                if not isinstance(value, str):
                    raise SdoConfigError(f"{name} must be a str, got {value!r}.")
                out.append((wire, value))
            elif kind == _ENUM:
                out.append((wire, self._enum(name, value)))
            else:  # _JSON_ARRAY
                if not isinstance(value, (list, tuple)):
                    raise SdoConfigError(f"{name} must be a list, got {type(value).__name__}.")
                out.append((wire, self._json(name, value)))

        self._check_dependencies(render_on)

        extra = self.extra_params
        if not _is_unset(extra):
            if not isinstance(extra, dict):
                raise SdoConfigError("extra_params must be a dict.")
            for key in extra:
                if not isinstance(key, str) or not key:
                    raise SdoConfigError(f"extra_params keys must be non-empty str, got {key!r}.")
            for key in sorted(extra):
                if key.lower() in _RESERVED_EXTRA:
                    raise SdoConfigError(
                        f"extra_params may not set {key!r}: the token comes from the client "
                        "and the target from ScrapeConfig.url."
                    )
                if not _PARAM_KEY_RE.fullmatch(key):
                    raise SdoConfigError(
                        f"extra_params key {key!r} is not a valid parameter name: use letters, "
                        "digits, '_', '.' or '-', starting with a letter."
                    )
                value = _extra_value(key, extra[key])
                if value is not None:
                    out.append((key, value))
        return out

    @staticmethod
    def _session_id(value: Any) -> str:
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            ok = False
        elif isinstance(value, int):
            ok = 0 <= value <= _MAX_SESSION_ID
        else:
            ok = bool(_SESSION_ID_RE.fullmatch(value)) and int(value) <= _MAX_SESSION_ID
        if not ok:
            raise SdoConfigError(
                "session_id must be an integer from 0 to 1000000 (an int or decimal "
                f"digits), got {value!r}."
            )
        return str(value)

    def _check_dependencies(self, render_on: bool) -> None:
        """The documented rules that tie parameters together, each of which the
        gateway rejects with a 400 (or, for ``zip_code`` and
        ``particular_screenshot`` with ``play_with_browser``, documents as
        invalid). Runs after every value has been type-checked."""

        def on(name: str) -> bool:
            return not _is_unset(getattr(self, name))

        if on("regional_geo_code"):
            if not on("super_proxy"):
                raise SdoConfigError("'regional_geo_code' requires super_proxy=True.")
            if on("geo_code"):
                raise SdoConfigError(
                    "'regional_geo_code' and 'geo_code' cannot be combined; set only one of them."
                )
        if on("zip_code") and not (on("super_proxy") and on("geo_code")):
            raise SdoConfigError(
                "'zip_code' requires super_proxy=True and a geo_code (the country of the "
                "postal code)."
            )
        if sum(on(n) for n in ("custom_headers", "extra_headers", "forward_headers")) > 1:
            raise SdoConfigError(
                "'custom_headers', 'extra_headers' and 'forward_headers' cannot be combined; "
                "set only one of them."
            )
        if on("retry_timeout") and render_on:
            raise SdoConfigError("'retry_timeout' cannot be combined with render.")
        if sum(on(n) for n in ("screenshot", "full_screenshot", "particular_screenshot")) > 1:
            raise SdoConfigError(
                "'screenshot', 'full_screenshot' and 'particular_screenshot' cannot be "
                "combined; set only one of them."
            )
        if not on("return_json"):
            for name in _NEEDS_RETURN_JSON:
                if on(name):
                    raise SdoConfigError(
                        f"'{name}' requires return_json=True (its output is part of the "
                        "JSON response)."
                    )
        if on("particular_screenshot") and on("play_with_browser"):
            raise SdoConfigError(
                "'particular_screenshot' cannot be combined with play_with_browser; add a "
                '{"Action": "ScreenShot", "particularScreenShot": "<selector>"} action instead.'
            )

    @staticmethod
    def _json(name: str, value: Any) -> str:
        try:
            return jdump(value)
        except (TypeError, ValueError) as exc:
            raise SdoConfigError(f"{name} is not JSON-serializable: {exc}") from None


__all__ = ["ScrapeConfig"]
