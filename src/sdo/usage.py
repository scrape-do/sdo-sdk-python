# SPDX-License-Identifier: Apache-2.0
"""Usage statistics: ``GET <base_url>/info`` (docs: API Response > Usage
Statistics)."""

from __future__ import annotations

from dataclasses import dataclass

from . import _json
from .errors import SdoTransportError

_COUNTS = (
    ("ConcurrentRequest", "concurrent_request"),
    ("MaxMonthlyRequest", "max_monthly_request"),
    ("RemainingConcurrentRequest", "remaining_concurrent_request"),
    ("RemainingMonthlyRequest", "remaining_monthly_request"),
)


@dataclass(frozen=True)
class Usage:
    """The account's usage snapshot. When the subscription is not active, the
    gateway reports every count as 0."""

    #: Whether the subscription is active.
    is_active: bool
    #: The plan's concurrent-request limit.
    concurrent_request: int
    #: The plan's monthly request limit.
    max_monthly_request: int
    #: How many more requests may run right now.
    remaining_concurrent_request: int
    #: How many requests are left this month.
    remaining_monthly_request: int


def parse_usage(body: bytes) -> Usage:
    """Read the ``/info`` body: a JSON object carrying every field,
    ``IsActive`` a bool and the counts integers."""

    def invalid(why: str) -> SdoTransportError:
        return SdoTransportError(f"The usage response is not the expected JSON object: {why}.")

    try:
        raw = _json.loads(body)
    except ValueError:
        raise invalid("not valid JSON") from None
    if not isinstance(raw, dict):
        raise invalid("not a JSON object")
    active = raw.get("IsActive")
    if not isinstance(active, bool):
        raise invalid("IsActive is missing or not a boolean")
    counts: dict[str, int] = {}
    for wire, field in _COUNTS:
        value = raw.get(wire)
        if isinstance(value, bool) or not isinstance(value, int):
            raise invalid(f"{wire} is missing or not an integer")
        counts[field] = value
    return Usage(is_active=active, **counts)


__all__ = ["Usage", "parse_usage"]
