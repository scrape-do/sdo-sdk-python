# SPDX-License-Identifier: Apache-2.0
"""``ScrapeResult`` — the response wrapper returned by a successful scrape.

The body is already transparently decompressed by the transport (gzip / deflate
/ brotli), so the accessors operate on decompressed bytes. ``status_code`` is
the *target's* status (a scrape of a 404 page is a successful ScrapeResult with
``status_code == 404``); only gateway-origin failures raise.
"""

from __future__ import annotations

import builtins
import re
from collections.abc import Iterable, Mapping
from typing import Any

from . import _json
from ._encoding import decode
from .errors import SdoTransportError

_UNSIGNED_RE = re.compile(r"[0-9]+", re.ASCII)
_SIGNED_RE = re.compile(r"-?[0-9]+", re.ASCII)


#: The largest cost read: 2**53 - 1, the largest integer every SDK holds exactly.
MAX_REQUEST_COST = 2**53 - 1


def parse_request_cost(raw: str | None) -> int | None:
    """``Scrape.do-Request-Cost`` as an int, or ``None`` when it is not one value
    of ASCII digits (spaces and tabs trimmed) of at most 2**53 - 1. Repeated
    headers arrive joined with ``", "`` and are refused."""
    cost = _parse_int(raw)
    return cost if cost is not None and cost <= MAX_REQUEST_COST else None


def _parse_int(raw: str | None, *, signed: bool = False) -> int | None:
    """An integer header value, or ``None``. ASCII digits only: ``str.isdigit``
    also accepts ``'²'`` and ``'١٢'``, which ``int()`` then rejects or misreads."""
    if raw is None:
        return None
    raw = raw.strip(" \t")
    if (_SIGNED_RE if signed else _UNSIGNED_RE).fullmatch(raw):
        return int(raw)
    return None


class ScrapeResult:
    """A successfully delivered scrape response."""

    #: The HTTP status code of the *target* page.
    status_code: int
    #: Response headers with lower-cased names. A header sent more than once is
    #: joined with ``", "``; use :meth:`header_values` to read each value.
    headers: dict[str, str]
    #: The URL that was scraped; ``""`` for a plugin result.
    url: str

    def __init__(
        self,
        *,
        content: bytes,
        status_code: int,
        headers: Mapping[str, str] | Iterable[tuple[str, str]],
        url: str,
    ) -> None:
        self._content = content
        self.status_code = status_code
        items = list(headers.items() if isinstance(headers, Mapping) else headers)
        self._header_list = [(k.lower(), v) for k, v in items]
        joined: dict[str, str] = {}
        for k, v in self._header_list:
            joined[k] = f"{joined[k]}, {v}" if k in joined else v
        self.headers = joined
        self.url = url
        self._text: str | None = None

    def header_values(self, name: str) -> list[str]:
        """Every value of header ``name`` (case-insensitive), in the order
        received. ``Set-Cookie`` cannot be split back out of the joined
        ``headers`` value, so read it here."""
        key = name.lower()
        return [v for k, v in self._header_list if k == key]

    # -- header-borne metadata (surfaced as typed accessors) -----------------
    @property
    def cost(self) -> int | None:
        """The credits charged (``Scrape.do-Request-Cost``, the authoritative
        figure), or ``None`` when the header is absent or unreadable."""
        return parse_request_cost(self.headers.get("scrape.do-request-cost"))

    @property
    def remaining_credits(self) -> int | None:
        return _parse_int(self.headers.get("scrape.do-remaining-credits"), signed=True)

    @property
    def content_type(self) -> str | None:
        return self.headers.get("content-type")

    @property
    def initial_status_code(self) -> int | None:
        """The target's first-hop status before any gateway retry/redirect
        handling (header ``scrape.do-initial-status-code``)."""
        return _parse_int(self.headers.get("scrape.do-initial-status-code"))

    @property
    def target_url(self) -> str | None:
        """The URL originally requested (header ``scrape.do-target-url``)."""
        return self.headers.get("scrape.do-target-url")

    @property
    def target_redirected_location(self) -> str | None:
        """The ``Location`` the target answered with when redirects were not
        followed (``disable_redirection``), from the header
        ``scrape.do-target-redirected-location``."""
        return self.headers.get("scrape.do-target-redirected-location")

    @property
    def resolved_url(self) -> str | None:
        """The final URL after redirects (header ``scrape.do-resolved-url``)."""
        return self.headers.get("scrape.do-resolved-url")

    @property
    def cookies(self) -> str | None:
        """Cookies surfaced by the gateway (header ``scrape.do-cookies``)."""
        return self.headers.get("scrape.do-cookies")

    # -- tri-modal body ------------------------------------------------------
    def bytes(self) -> builtins.bytes:
        """The raw, decompressed payload (transparent decompression)."""
        return self._content

    @property
    def text(self) -> str:
        """The body decoded per the WHATWG Encoding Standard: a BOM, then the
        Content-Type ``charset``, then UTF-8 for JSON, then an HTML ``<meta>``
        charset in the first 1024 bytes, then UTF-8. Invalid bytes become
        U+FFFD. Only labels of the Encoding Standard are honoured."""
        if self._text is None:
            self._text = decode(self._content, self.content_type)
        return self._text

    def json(self, **kwargs: Any) -> Any:
        """Parse the body as JSON: UTF-8 with invalid bytes replaced by U+FFFD,
        a leading BOM ignored. ``NaN``, ``Infinity`` and numbers that overflow
        a double are rejected. ``kwargs`` are passed to :func:`json.loads`.

        Raises :class:`~sdo.SdoTransportError` when the body is not valid JSON
        (nesting too deep to parse included), like every other SDK failure.
        """
        try:
            return _json.loads(self._content, **kwargs)
        except ValueError:
            raise SdoTransportError("The response body is not valid JSON.") from None

    def selector(self) -> Any:
        """Return a ``parsel.Selector`` for CSS/XPath extraction (typed ``Any``
        so the optional dependency is not needed to resolve the annotation).

        ``parsel`` is an optional dependency; install ``sdo-sdk[parse]``.
        """
        try:
            from parsel import Selector
        except ImportError as exc:  # pragma: no cover - optional dep
            raise ImportError(
                "selector() needs the optional 'parsel' dependency: pip install 'sdo-sdk[parse]'"
            ) from exc
        return Selector(text=self.text)

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (
            f"ScrapeResult(status_code={self.status_code}, "
            f"bytes={len(self._content)}, url={self.url!r})"
        )


__all__ = ["ScrapeResult"]
