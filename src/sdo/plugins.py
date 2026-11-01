# SPDX-License-Identifier: Apache-2.0
"""Generic plugin core.

A single ``call_plugin`` builds ``/plugin/<route>?token=...&<params>`` and
returns the parsed JSON (or raw bytes for non-JSON plugins). This is the
hand-written generic core: a brand-new documented plugin is callable the day it
ships, before any typed binding exists.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from . import _json
from ._wire import es_number, jdump, query
from .errors import SdoConfigError, SdoTransportError

if TYPE_CHECKING:
    from ._transport import Reply, Transport

#: Plugin routes reach this function from callers — including tools where a
#: model chooses the value. Validating before it enters the URL path stops
#: traversal ("../"), query injection ("x?token=") and fragment truncation
#: ("x#"). fullmatch, because "$" also matches before a trailing newline.
_ROUTE_RE = re.compile(r"^[a-z0-9]+(?:[/-][a-z0-9]+)*$")
#: Keys a caller may not set (compared case-insensitively): the token only
#: comes from the client.
_RESERVED = ("token",)
#: The shape of a parameter name: an ASCII letter, then letters, digits, ``_``,
#: ``.`` or ``-`` (as for ``ScrapeConfig.extra_params``).
_KEY_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.\-]*", re.ASCII)
_DIGITS_RE = re.compile(r"[0-9]+", re.ASCII)


def _scalar(key: str, value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"  # literal, unlike ScrapeConfig flags
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise SdoConfigError(f"Plugin parameter {key!r} must be a finite number.")
        return es_number(value)
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        try:
            return jdump(dict(value))  # an object is sent as canonical JSON
        except (TypeError, ValueError) as exc:
            raise SdoConfigError(f"Plugin parameter {key!r} is not JSON: {exc}") from None
    raise SdoConfigError(
        f"Plugin parameter {key!r} has an unsupported type {type(value).__name__}."
    )


def serialize(params: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Sorted by key (code point); a list repeats its key per element (a nested
    list is an error); an object is canonical JSON; ``None`` is skipped."""
    for key in params:
        if not isinstance(key, str) or not key:
            raise SdoConfigError(f"Plugin parameter names must be non-empty str, got {key!r}.")
        if key.lower() in _RESERVED:
            raise SdoConfigError(
                f"Plugin parameters may not set {key!r}: the token comes from the client."
            )
        if not _KEY_RE.fullmatch(key):
            raise SdoConfigError(
                f"Plugin parameter name {key!r} is not valid: use letters, digits, '_', '.' "
                "or '-', starting with a letter."
            )
    out: list[tuple[str, str]] = []
    for key in sorted(params):
        value = params[key]
        items = value if isinstance(value, (list, tuple)) else [value]
        for item in items:
            if isinstance(item, (list, tuple)):
                raise SdoConfigError(f"Plugin parameter {key!r} cannot be a nested list.")
            if item is not None:
                out.append((key, _scalar(key, item)))
    return out


#: Routes the gateway serves under a different path. The Amazon raw-HTML
#: plugin is the group root ``/plugin/amazon/``, and the gateway answers
#: ``/plugin/amazon`` with a redirect the SDK never follows; its documented
#: spelling ``"amazon/"`` works too.
_ROUTE_PATHS = {"amazon": "amazon/", "amazon/": "amazon/"}


def validate_route(route: str) -> str:
    """The validated path under ``/plugin/`` for ``route``."""
    if isinstance(route, str) and route in _ROUTE_PATHS:
        return _ROUTE_PATHS[route]
    if not isinstance(route, str) or not _ROUTE_RE.fullmatch(route):
        raise SdoConfigError(
            f"Invalid plugin route {route!r}. Routes look like 'google/shopping': "
            "lowercase letters and digits, separated by '/' or '-'."
        )
    return route


def plugin_timeout_ms(pairs: list[tuple[str, str]]) -> int | None:
    """The plugin's ``timeout`` param (milliseconds) when it is sent once as a
    positive integer (ASCII digits), else ``None``."""
    values = [v for k, v in pairs if k == "timeout"]
    if len(values) != 1 or not _DIGITS_RE.fullmatch(values[0]):
        return None
    return int(values[0]) or None


def plugin_reply(
    transport: Transport, base_url: str, route: str, params: Mapping[str, Any]
) -> Reply:
    """Call a plugin endpoint generically and return the delivered reply
    (status, headers and body). The transport adds the token."""
    path = "/plugin/" + validate_route(route)
    try:
        pairs = serialize(params)
        qs = query(pairs)
    except UnicodeEncodeError:
        raise SdoConfigError("Plugin parameters must be valid Unicode text.") from None
    # Like a scrape's ``timeout``, the plugin's own waits for that plus a margin.
    return transport.send(
        "GET", f"{base_url}{path}", query=qs, gateway_timeout_ms=plugin_timeout_ms(pairs)
    )


def call_plugin(transport: Transport, base_url: str, route: str, params: Mapping[str, Any]) -> Any:
    """Call a plugin endpoint generically. Returns parsed JSON, else raw bytes.
    The transport adds the token."""
    reply = plugin_reply(transport, base_url, route, params)
    content_type = (reply.header("content-type") or "").lower()
    if "json" not in content_type:
        return reply.body
    try:
        return _json.loads(reply.body)
    except ValueError:
        pass
    raise SdoTransportError(f"The {route} plugin returned a body that is not valid JSON.")


__all__ = ["call_plugin", "plugin_reply"]
