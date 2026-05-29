"""Behavior tests for RecoveryController.

Groups:
- TestRateLimit: cooldown + rate-limit/circuit-breaker gating of resets.
- TestResetCycle: the async select-walk and its error handling.
- TestOffFallback: parking-in-off recovery.
- TestDiagnostics: snapshot fields the status sensor reads.

How to run:
    PYTHONPATH=. pytest custom_components/sanitized_presence/tests/test_recovery.py
"""

from __future__ import annotations

# pylint: disable=protected-access

from unittest.mock import MagicMock

import pytest

from custom_components.sanitized_presence.const import (
    RESET_COOLDOWN_SEC,
    RESET_RATE_BLOCK_SEC,
    RESET_RATE_LIMIT,
)
from custom_components.sanitized_presence.recovery import RecoveryController


def _make_controller(hass=None):
    return RecoveryController(
        hass=hass or MagicMock(),
        device_id="dev1",
        device_name="Radar 1",
        sensor_eid="select.radar_sensor",
    )


class TestRateLimit:
    """Cooldown and rate-limit/circuit-breaker decisions are time-pure."""

    def test_allows_first_reset(self):
        """A fresh controller permits an immediate reset.

        Validates: no spurious cooldown/circuit-breaker blocks a device
        that has never been reset.
        Code: custom_components/sanitized_presence/recovery.py::RecoveryController._allow_reset
        Assertion: _allow_reset(now=1000.0) is True.
        Method:
        1. Arrange: fresh controller.
        2. Act: call _allow_reset(1000.0).
        3. Assert: True.
        """
        ctrl = _make_controller()
        assert ctrl._allow_reset(now=1000.0) is True

    def test_cooldown_blocks_back_to_back(self):
        """A reset within RESET_COOLDOWN_SEC of the last one is blocked.

        Validates: the integration never hammers the select entity twice
        in quick succession.
        Code: custom_components/sanitized_presence/recovery.py::RecoveryController._allow_reset
        Assertion: a reset RESET_COOLDOWN_SEC-1 after the last is blocked;
            one exactly at the boundary is allowed.
        Method:
        1. Arrange: record a reset at t=1000.
        2. Act: query _allow_reset just inside and just past cooldown.
        3. Assert: inside -> False, past -> True.
        """
        ctrl = _make_controller()
        ctrl._record_reset(1000.0)
        assert ctrl._allow_reset(now=1000.0 + RESET_COOLDOWN_SEC - 1) is False
        assert ctrl._allow_reset(now=1000.0 + RESET_COOLDOWN_SEC) is True

    def test_circuit_breaker_trips_after_rate_limit(self):
        """Exceeding RESET_RATE_LIMIT resets in the window trips the breaker.

        Validates: a runaway reset loop is capped to protect the Zigbee
        mesh; once tripped, further resets are blocked for the block window.
        Code: custom_components/sanitized_presence/recovery.py::RecoveryController._allow_reset
        Assertion: after RESET_RATE_LIMIT recorded resets, the next
            _allow_reset is False, and remains False until
            RESET_RATE_BLOCK_SEC elapses.
        Method:
        1. Arrange: record RESET_RATE_LIMIT resets spaced past cooldown.
        2. Act: query _allow_reset right after, and after the block window.
        3. Assert: blocked, then allowed.
        """
        ctrl = _make_controller()
        t = 1000.0
        for _ in range(RESET_RATE_LIMIT):
            ctrl._record_reset(t)
            t += RESET_COOLDOWN_SEC  # spaced so cooldown alone wouldn't block
        blocked_at = t
        assert ctrl._allow_reset(now=blocked_at) is False
        assert ctrl._allow_reset(now=blocked_at + RESET_RATE_BLOCK_SEC) is True
