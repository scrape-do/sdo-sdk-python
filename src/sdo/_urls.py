# SPDX-License-Identifier: Apache-2.0
"""The URL grammar every SDK applies, as a string check.

A URL parser's leniency is exactly what this must not inherit: WHATWG parsers
accept ``https:e.com`` and ``https:\\e.com``, and ``urlsplit`` accepts spaces
and an out-of-range port. The grammar is::

    scheme "://" authority [ path-query-fragment ]

with ``scheme`` http or https in any case, ``authority`` everything up to the
first ``/``, ``?`` or ``#`` (optional ``userinfo@``, a non-empty host, an
optional port 1-65535), and no whitespace, control character or backslash
anywhere.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

#: The userinfo of a URL (whatever sits before the last ``@`` of the authority).
_USERINFO_RE = re.compile(r"^([^/]*//)[^/?#]*@", re.DOTALL)
_PREFIX_RE = re.compile(r"(https?)://([^/?#]*)(.*)", re.IGNORECASE | re.DOTALL | re.ASCII)
_PORT_RE = re.compile(r"[0-9]{1,5}", re.ASCII)


@dataclass(frozen=True)
class Parts:
    scheme: str  # lower-cased
    userinfo: str | None
    host: str  # without the IPv6 brackets
    rest: str  # path, query and fragment, as given


def _clean(url: str) -> bool:
    return not any(ch == "\\" or ch.isspace() or unicodedata.category(ch) == "Cc" for ch in url)


def split(url: object) -> Parts | None:
    """The parts of ``url``, or ``None`` when it breaks the grammar."""
    if not isinstance(url, str) or not _clean(url):
        return None
    m = _PREFIX_RE.fullmatch(url)
    if m is None:
        return None
    scheme, authority, rest = m.groups()
    userinfo: str | None
    userinfo, at, hostport = authority.rpartition("@")
    if not at:
        userinfo = None
    if hostport.startswith("["):
        close = hostport.find("]")
        if close < 0:
            return None
        host, port = hostport[1:close], hostport[close + 1 :]
        if port and not port.startswith(":"):
            return None
        port = port[1:] if port else None
    else:
        host, colon, port_text = hostport.partition(":")
        port = port_text if colon else None
    if not host:
        return None
    if port is not None and not (_PORT_RE.fullmatch(port) and 1 <= int(port) <= 65535):
        return None
    return Parts(scheme.lower(), userinfo, host, rest)


def shown(url: object) -> str:
    """``repr(url)`` for an error message, with any ``user:password@`` masked
    and credential-shaped values (``token=``, ``api_key=``, ...) redacted."""
    from .errors import redact_secrets  # errors does not import this module

    if not isinstance(url, str):
        return repr(url)
    return repr(redact_secrets(_USERINFO_RE.sub(r"\1***@", url)))


__all__ = ["Parts", "shown", "split"]
