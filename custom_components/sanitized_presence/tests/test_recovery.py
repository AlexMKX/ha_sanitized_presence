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


class TestResetCycle:
    """request_reset drives the select through SENSOR_RESET_SEQUENCE."""

    @pytest.mark.asyncio
    async def test_reset_walks_sequence_in_order(self, monkeypatch):
        """A reset calls select.select_option for each phase in order.

        Validates: the firmware-recovery contract — the select is walked
        off -> unoccupied -> on. The order IS the contract, so exact call
        order is asserted intentionally.
        Code: custom_components/sanitized_presence/recovery.py::RecoveryController.async_reset
        Assertion: hass.services.async_call invoked once per sequence
            option, in SENSOR_RESET_SEQUENCE order, on the sensor eid.
        Method:
        1. Arrange: controller with a mock hass; patch asyncio.sleep.
        2. Act: await async_reset("test").
        3. Assert: call options equal list(SENSOR_RESET_SEQUENCE).
        """
        import custom_components.sanitized_presence.recovery as rec
        from custom_components.sanitized_presence.const import SENSOR_RESET_SEQUENCE

        async def _no_sleep(_):
            return None

        monkeypatch.setattr(rec.asyncio, "sleep", _no_sleep)

        hass = MagicMock()
        calls = []

        async def _async_call(domain, service, data, blocking=False):
            calls.append((domain, service, data["option"], data["entity_id"]))

        hass.services.async_call = _async_call
        ctrl = _make_controller(hass)

        await ctrl.async_reset("test")

        assert [c[2] for c in calls] == list(SENSOR_RESET_SEQUENCE)
        assert all(c[3] == "select.radar_sensor" for c in calls)
        assert all(c[0] == "select" and c[1] == "select_option" for c in calls)

    @pytest.mark.asyncio
    async def test_reset_blocked_by_cooldown_does_not_call_service(self, monkeypatch):
        """request_reset within cooldown performs no select_option calls.

        Validates: the cooldown rail actually prevents side effects, not
        just returns False.
        Code: custom_components/sanitized_presence/recovery.py::RecoveryController.request_reset
        Assertion: with a recent reset recorded, request_reset returns
            False and async_call is never invoked.
        Method:
        1. Arrange: record a reset at "now"; freeze time just after.
        2. Act: await request_reset("test").
        3. Assert: returns False; no service calls.
        """
        import custom_components.sanitized_presence.recovery as rec

        monkeypatch.setattr(rec.time, "time", lambda: 1000.0)
        hass = MagicMock()
        calls = []

        async def _async_call(*args, **kwargs):
            calls.append(args)

        hass.services.async_call = _async_call
        ctrl = _make_controller(hass)
        ctrl._record_reset(1000.0)

        started = await ctrl.request_reset("test")

        assert started is False
        assert calls == []

    @pytest.mark.asyncio
    async def test_reset_failure_aborts_and_clears_resetting(self, monkeypatch):
        """A failing select_option aborts the cycle and clears is_resetting.

        Validates: fail-fast error handling — a service error does not
        leave the echo-suppression gate stuck closed, and remaining phases
        are not attempted after the failure.
        Code: custom_components/sanitized_presence/recovery.py::RecoveryController.async_reset
        Assertion: async_reset raises/handles, is_resetting is False after,
            and no phases run past the failing one.
        Method:
        1. Arrange: async_call raises HomeAssistantError on first call.
        2. Act: await async_reset("test").
        3. Assert: is_resetting False; exactly one call attempted.
        """
        import custom_components.sanitized_presence.recovery as rec
        from homeassistant.exceptions import HomeAssistantError

        async def _no_sleep(_):
            return None

        monkeypatch.setattr(rec.asyncio, "sleep", _no_sleep)
        hass = MagicMock()
        calls = []

        async def _async_call(*args, **kwargs):
            calls.append(args)
            raise HomeAssistantError("boom")

        hass.services.async_call = _async_call
        ctrl = _make_controller(hass)

        await ctrl.async_reset("test")

        assert ctrl.is_resetting is False
        assert len(calls) == 1


class TestOffFallback:
    """maybe_recover_off flips a select parked in 'off' back to 'on'."""

    @pytest.mark.asyncio
    async def test_off_select_is_restored_to_on(self):
        """A select reading 'off' (no cycle running) is restored to 'on'.

        Validates: the last-resort guard recovers a cycle interrupted by an
        integration restart, where the select would otherwise stay 'off'.
        Code: custom_components/sanitized_presence/recovery.py::RecoveryController.maybe_recover_off
        Assertion: select_option('on') is called once.
        Method:
        1. Arrange: hass.states.get(sensor_eid).state == 'off'; not resetting.
        2. Act: await maybe_recover_off().
        3. Assert: one select_option call with option 'on'.
        """
        hass = MagicMock()
        state = MagicMock()
        state.state = "off"
        hass.states.get.return_value = state
        calls = []

        async def _async_call(domain, service, data, blocking=False):
            calls.append(data["option"])

        hass.services.async_call = _async_call
        ctrl = _make_controller(hass)

        await ctrl.maybe_recover_off()

        assert calls == ["on"]

    @pytest.mark.asyncio
    async def test_off_fallback_skipped_while_resetting(self):
        """The fallback does not interfere with an in-flight reset cycle.

        Validates: maybe_recover_off respects the re-entrancy guard so it
        never collides with the legitimate 'off' phase of a running cycle.
        Code: custom_components/sanitized_presence/recovery.py::RecoveryController.maybe_recover_off
        Assertion: with is_resetting True, no service call is made even if
            the select reads 'off'.
        Method:
        1. Arrange: select state 'off'; set _resetting True.
        2. Act: await maybe_recover_off().
        3. Assert: no service calls.
        """
        hass = MagicMock()
        state = MagicMock()
        state.state = "off"
        hass.states.get.return_value = state
        calls = []

        async def _async_call(*args, **kwargs):
            calls.append(args)

        hass.services.async_call = _async_call
        ctrl = _make_controller(hass)
        ctrl._resetting = True

        await ctrl.maybe_recover_off()

        assert calls == []


class TestDiagnostics:
    """diagnostics() reports the safety-rail state the status sensor shows."""

    def test_diagnostics_reports_cooldown_and_counts(self):
        """After a reset, diagnostics shows cooldown-left and rate count.

        Validates: the status sensor receives accurate, current safety-rail
        figures (cooldown remaining, resets in the window).
        Code: custom_components/sanitized_presence/recovery.py::RecoveryController.diagnostics
        Assertion: immediately after a reset at t=1000, diagnostics(now=1000)
            shows cooldown_left==RESET_COOLDOWN_SEC and rate_window_count==1.
        Method:
        1. Arrange: controller; record a reset at t=1000.
        2. Act: diagnostics(now=1000).
        3. Assert: cooldown_left and rate_window_count as expected.
        """
        ctrl = _make_controller()
        ctrl._record_reset(1000.0)
        diag = ctrl.diagnostics(now=1000.0)
        assert diag["cooldown_left"] == RESET_COOLDOWN_SEC
        assert diag["rate_window_count"] == 1
        assert diag["resetting"] is False
