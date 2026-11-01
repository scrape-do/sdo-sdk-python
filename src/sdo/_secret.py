# SPDX-License-Identifier: Apache-2.0
"""The token, held so that no stack frame shows it.

Error trackers (Sentry's ``include_local_variables``, pytest ``--showlocals``,
rich tracebacks, the Django debug page) record the ``repr()`` of every local
variable of every frame an exception passes through. So the token never sits in
a plain local on a path that can raise: it lives in a :class:`Secret` whose
``repr`` and ``str`` are masked, and the raw value is only read inside small
helpers that return before anything is raised.
"""

from __future__ import annotations

import os
import re

#: Tokens are trimmed of ASCII whitespace (WHATWG: tab, LF, FF, CR, space).
_ASCII_WHITESPACE = " \t\n\f\r"
#: What is left must be visible ASCII: it travels in a query string and a header.
_TOKEN_RE = re.compile(r"[\x21-\x7e]+")


class Secret:
    """A credential whose ``repr`` and ``str`` never show it."""

    __slots__ = ("_value",)

    def __init__(self, value: object) -> None:
        # Any caller argument is wrapped as-is, before it is validated, so even
        # an invalid token never sits in a plain local.
        self._value = value

    def reveal(self) -> str:
        value = self._value
        return value if isinstance(value, str) else ""

    def __repr__(self) -> str:
        return "Secret('***')"

    __str__ = __repr__


def _clean(raw: object) -> str | None:
    """The trimmed token, ``""`` when blank, or ``None`` when it is invalid."""
    if not isinstance(raw, str):
        return None
    value = raw.strip(_ASCII_WHITESPACE)
    if value and not _TOKEN_RE.fullmatch(value):
        return None
    return value


def resolve_token(arg: Secret) -> Secret | str:
    """The token from the argument, else ``SDO_TOKEN``, wrapped; or the text of
    the configuration error to raise. A blank argument means "not given".

    Returns rather than raises, so the frame that raises holds no raw value.
    """
    raw = arg._value
    if raw is not None:
        value = _clean(raw)
        if value is None:
            return "The token must be a string of visible ASCII characters."
        if value:
            return Secret(value)
    env = _clean(os.environ.get("SDO_TOKEN", ""))
    if env is None:
        return "SDO_TOKEN must be a string of visible ASCII characters."
    if env:
        return Secret(env)
    return "No token provided. Pass Client(token=...) or set SDO_TOKEN."


__all__ = ["Secret", "resolve_token"]
