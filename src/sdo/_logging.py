# SPDX-License-Identifier: Apache-2.0
"""Keep the token out of httpx/httpcore log records.

httpx logs every request at INFO as ``HTTP Request: GET <full URL>``, and the
sync API carries the token in that URL's query string, so any application that
logs at INFO (``logging.basicConfig(level=logging.INFO)``, Celery ``-l info``,
Scrapy's DEBUG default) would write the live token to its logs. A filter on
those loggers rewrites each record through the same redaction as error
messages, plus a literal scrub of every token a live client holds.

Filters on a logger do not apply to its children, so each logger httpcore
(and hpack, for HTTP/2) creates is covered by name.
"""

from __future__ import annotations

import logging
import threading

from .errors import redact_secrets

_LOGGERS = (
    "httpx",
    "httpcore",
    "httpcore.connection",
    "httpcore.http11",
    "httpcore.http2",
    "httpcore.proxy",
    "httpcore.socks",
    # HTTP/2 (httpx[http2]): hpack logs header blocks and evicted header-table
    # entries, X-Token among them, at DEBUG. h2 itself logs through a no-op
    # logger unless one is configured, and httpcore configures none.
    "hpack",
    "hpack.hpack",
    "hpack.table",
)

_lock = threading.Lock()
_tokens: dict[str, int] = {}


def register_token(token: str) -> None:
    """Scrub ``token`` literally from log records while a client uses it."""
    with _lock:
        _tokens[token] = _tokens.get(token, 0) + 1


def unregister_token(token: str) -> None:
    with _lock:
        count = _tokens.get(token, 0) - 1
        if count > 0:
            _tokens[token] = count
        else:
            _tokens.pop(token, None)


def redact_known(text: str) -> str:
    """Redact credential shapes and every registered token from ``text``."""
    with _lock:
        tokens = sorted(_tokens, key=len, reverse=True)
    for token in tokens:
        text = redact_secrets(text, token)
    return redact_secrets(text)


class RedactingFilter(logging.Filter):
    """Rewrites a record's message with credentials masked."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
            broken = False
        except Exception:  # a malformed record: keep its parts, masked
            message = f"{record.msg} {record.args!r}"
            broken = True
        masked = redact_known(message)
        if broken or masked != message:
            record.msg = masked
            record.args = None
        return True


def install() -> None:
    """Attach the filter once per logger (idempotent across re-imports)."""
    for name in _LOGGERS:
        logger = logging.getLogger(name)
        if not any(type(f).__name__ == "RedactingFilter" for f in logger.filters):
            logger.addFilter(RedactingFilter())


install()

__all__ = ["RedactingFilter", "install", "redact_known", "register_token", "unregister_token"]
