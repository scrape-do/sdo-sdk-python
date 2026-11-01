# SPDX-License-Identifier: Apache-2.0
"""The canonical wire encodings shared by every SDK.

Written by hand because no standard library in the three SDK languages produces
them unaided: ``json.dumps`` prints ``1.0`` and ``1e-07``, and ``urlencode``
turns a space into ``+`` and keeps ``*``. Every function here must stay
byte-for-byte identical to the JS and Go SDKs; the conformance corpus pins them.
"""

from __future__ import annotations

import decimal
import math
from typing import Any


def es_number(x: int | float) -> str:
    """ECMAScript ``Number::toString`` — what ``JSON.stringify`` prints."""
    if isinstance(x, bool):
        raise TypeError("bool is not a number")
    if isinstance(x, int):
        return str(x)
    if math.isnan(x) or math.isinf(x):
        raise ValueError("NaN and Infinity are not JSON numbers")
    if x == 0:
        return "0"  # also -0
    sign = "-" if x < 0 else ""
    # repr() is the shortest round-tripping decimal, as ECMAScript requires.
    t = decimal.Decimal(repr(abs(x))).as_tuple()
    digits = "".join(map(str, t.digits)).lstrip("0")
    exp = int(t.exponent)
    stripped = digits.rstrip("0")
    exp += len(digits) - len(stripped)
    digits = stripped
    k = len(digits)
    n = k + exp  # value = 0.digits * 10^n
    if k <= n <= 21:
        out = digits + "0" * (n - k)
    elif 0 < n <= 21:
        out = digits[:n] + "." + digits[n:]
    elif -6 < n <= 0:
        out = "0." + "0" * (-n) + digits
    else:
        e = n - 1
        mant = digits if k == 1 else digits[0] + "." + digits[1:]
        out = f"{mant}e{'+' if e >= 0 else '-'}{abs(e)}"
    return sign + out


_SHORT_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


def _json_string(s: str) -> str:
    out = ['"']
    for ch in s:
        o = ord(ch)
        if ch in _SHORT_ESCAPES:
            out.append(_SHORT_ESCAPES[ch])
        elif o < 0x20 or 0xD800 <= o <= 0xDFFF:
            out.append(f"\\u{o:04x}")
        else:
            out.append(ch)  # includes U+2028/U+2029, DEL, <, >, &
    out.append('"')
    return "".join(out)


def jdump(value: Any) -> str:
    """The canonical JSON encoding for JSON-valued params and JSON bodies.

    Compact; object keys sorted by Unicode code point (Python's ``str`` order);
    no HTML escaping; U+2028/U+2029 literal; ECMAScript numbers. Raises
    :class:`TypeError` or :class:`ValueError` for a value JSON cannot express
    (a cyclic or too deeply nested one included).
    """
    try:
        return _jdump(value)
    except RecursionError:
        raise ValueError("the value is cyclic or nested too deeply") from None


def _jdump(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return es_number(value)
    if isinstance(value, str):
        return _json_string(value)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_jdump(v) for v in value) + "]"
    if isinstance(value, dict):
        for k in value:
            if not isinstance(k, str):
                raise TypeError(f"JSON object keys must be strings, got {type(k).__name__}")
        return "{" + ",".join(_json_string(k) + ":" + _jdump(value[k]) for k in sorted(value)) + "}"
    raise TypeError(f"not JSON-serializable: {type(value).__name__}")


_UNRESERVED = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")


def pct(s: str) -> str:
    """Percent-encode a query component: RFC 3986 unreserved bytes stay literal,
    every other UTF-8 byte becomes ``%XX`` (uppercase); a space is ``%20``."""
    return "".join(chr(b) if b in _UNRESERVED else f"%{b:02X}" for b in s.encode("utf-8"))


def query(pairs: list[tuple[str, str]]) -> str:
    """Join already-ordered ``(key, value)`` pairs into the exact query string
    that goes on the wire. Built here rather than by httpx, which regroups
    repeated keys and would reorder the pairs."""
    return "&".join(pct(k) + "=" + pct(v) for k, v in pairs)


__all__ = ["es_number", "jdump", "pct", "query"]
