# SPDX-License-Identifier: Apache-2.0
"""The one JSON reader for bodies the SDK parses: ``ScrapeResult.json()``,
plugin responses, async-job responses and error envelopes.

UTF-8 with invalid bytes replaced by U+FFFD, a leading BOM ignored; ``NaN``,
``Infinity`` and numbers that overflow a double are rejected (they are not JSON,
and the JS and Go SDKs refuse them); any failure, nesting too deep for the
parser included, raises :class:`ValueError`.
"""

from __future__ import annotations

import json
import math
from typing import Any


def _no_constant(name: str) -> Any:
    raise ValueError(f"{name} is not a JSON number")


def _float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"{text} overflows a double")
    return value


def _int(text: str) -> int:
    value = int(text)
    float(value)  # OverflowError when it cannot be a double
    return value


def loads(body: bytes, **kwargs: Any) -> Any:
    """Parse ``body``; raise :class:`ValueError` when it is not valid JSON."""
    text = body.decode("utf-8", errors="replace").removeprefix("﻿")
    options: dict[str, Any] = {
        "parse_constant": _no_constant,
        "parse_float": _float,
        "parse_int": _int,
    }
    options.update(kwargs)
    try:
        return json.loads(text, **options)
    except (RecursionError, OverflowError) as exc:
        raise ValueError(f"{type(exc).__name__}: {exc}") from None


__all__ = ["loads"]
