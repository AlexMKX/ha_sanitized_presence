"""Pure helper functions for Sanitized Presence.

No HA imports — these are safe to unit-test without any mocking.
"""

from __future__ import annotations

from typing import Any


def _to_float(value: Any) -> float | None:
    """Convert an HA state string / number to float; return None if unparseable."""
    if value in (None, "", "unknown", "unavailable"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clamp(value: float, lo: float, hi: float) -> float:
    """Clamp value to [lo, hi]."""
    return max(lo, min(hi, value))


def in_range(
    target: float,
    shield: float,
    detect: float,
    shield_floor: float,
) -> bool:
    """Return True iff target is strictly inside (effective_min, detect).

    effective_min = max(shield, shield_floor).
    Both bounds are exclusive (strict inequalities).
    """
    effective_min = max(shield, shield_floor)
    return effective_min < target < detect
