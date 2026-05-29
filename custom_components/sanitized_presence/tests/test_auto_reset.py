"""Behavior tests for AutoResetBinarySensor.

Covers the sliding-window deadline contract that all sanitized_presence
binary sensors rely on:
- pulse() turns the sensor on and schedules a reset-to-off after the
  effective timeout.
- A second pulse() cancels the previous reset (sliding window).
- The scheduled callback flips the sensor back to off.
- Entity removal cancels any pending reset.
- pulse(timeout=X) overrides the ctor timeout (used by SanitizedPresence
  to apply the live departure_delay).
- _notify_deadline is invoked on every state-transition so the companion
  DeadlineSensorEntity reflects current expiry.

How to run:
    pytest custom_components/sanitized_presence/tests/test_auto_reset.py
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from custom_components.sanitized_presence.auto_reset import AutoResetBinarySensor


class _Probe(AutoResetBinarySensor):
    """Concrete subclass used by the tests; records observable side effects."""

    def __init__(self, hass, reset_timeout: float) -> None:
        super().__init__(hass, reset_timeout)
        self.state_writes = 0
        self.deadline_calls: list = []

    def async_write_ha_state(self) -> None:  # pylint: disable=overridden-final-method
        """Record a state write instead of calling HA."""
        self.state_writes += 1

    def _notify_deadline(self, expiry_dt) -> None:
        """Record the deadline notification instead of forwarding to a sensor."""
        self.deadline_calls.append(expiry_dt)


class TestAutoResetBinarySensorBase:
    """Sliding-window contract: pulse on, scheduled reset off."""

    def test_pulse_turns_on_and_schedules_reset(self, hass):
        """pulse() flips is_on True, schedules a reset, and writes state.

        Validates: the basic sliding-window contract that downstream code
        and the HA state machine depend on.
        Code: custom_components/sanitized_presence/auto_reset.py::AutoResetBinarySensor.pulse
        Assertion: is_on is True, async_call_later is invoked with the
            ctor timeout, async_write_ha_state runs exactly once.
        Method:
        1. Arrange: patch async_call_later; build _Probe(reset_timeout=15).
        2. Act: call pulse().
        3. Assert: is_on True; second positional arg of async_call_later
            equals 15; state_writes == 1.
        """
        cancel = MagicMock()
        with patch(
            "custom_components.sanitized_presence.auto_reset.async_call_later",
            return_value=cancel,
        ) as mock_later:
            sensor = _Probe(hass, reset_timeout=15)
            sensor.pulse()

            assert sensor.is_on is True
            assert mock_later.call_args[0][1] == 15
            assert sensor.state_writes == 1

    def test_second_pulse_cancels_previous_reset(self, hass):
        """A second pulse() cancels the previously scheduled reset.

        Validates: sliding-window semantics. Without cancel, two timers
        would race and the first to fire could flip the sensor off while
        the radar is still detecting a target.
        Code: custom_components/sanitized_presence/auto_reset.py::AutoResetBinarySensor.pulse
        Assertion: the first scheduled cancel callable is invoked exactly
            once; is_on remains True.
        Method:
        1. Arrange: patch async_call_later to return two cancel mocks.
        2. Act: pulse() twice.
        3. Assert: first cancel was called once; is_on True.
        """
        cancel_first = MagicMock()
        cancel_second = MagicMock()
        with patch(
            "custom_components.sanitized_presence.auto_reset.async_call_later",
            side_effect=[cancel_first, cancel_second],
        ):
            sensor = _Probe(hass, reset_timeout=15)
            sensor.pulse()
            sensor.pulse()

            cancel_first.assert_called_once()
            assert sensor.is_on is True

    def test_scheduled_reset_callback_turns_sensor_off(self, hass):
        """When the timer expires, the sensor flips to off.

        Validates: the off-edge that consumer integrations (lights,
        groups) react to. If the callback never flipped the sensor, the
        deadline would be silent.
        Code: custom_components/sanitized_presence/auto_reset.py::AutoResetBinarySensor._on_reset
        Assertion: is_on becomes False and a second write happens for the
            off transition.
        Method:
        1. Arrange: capture the callback passed to async_call_later.
        2. Act: pulse() then invoke the captured callback.
        3. Assert: is_on False; state_writes == 2 (one per transition).
        """
        captured: dict = {}
        with patch(
            "custom_components.sanitized_presence.auto_reset.async_call_later",
            side_effect=lambda _h, _d, cb: captured.__setitem__("cb", cb) or MagicMock(),
        ):
            sensor = _Probe(hass, reset_timeout=15)
            sensor.pulse()
            captured["cb"](None)

            assert sensor.is_on is False
            assert sensor.state_writes == 2

    async def test_entity_removal_cancels_pending_reset(self, hass):
        """async_will_remove_from_hass cancels any pending reset.

        Validates: no orphan timers after entity removal (e.g. when the
        integration is reloaded). An orphan timer would fire on a stale
        entity and raise.
        Code: custom_components/sanitized_presence/auto_reset.py::AutoResetBinarySensor.async_will_remove_from_hass
        Assertion: the cancel callable returned by async_call_later runs
            once on removal.
        Method:
        1. Arrange: patch async_call_later to return a single cancel mock.
        2. Act: pulse() then await async_will_remove_from_hass().
        3. Assert: cancel called once.
        """
        cancel = MagicMock()
        with patch(
            "custom_components.sanitized_presence.auto_reset.async_call_later",
            return_value=cancel,
        ):
            sensor = _Probe(hass, reset_timeout=15)
            sensor.pulse()
            await sensor.async_will_remove_from_hass()

            cancel.assert_called_once()


class TestDynamicPulseTimeout:
    """pulse(timeout=X) overrides ctor reset_timeout for one pulse."""

    def test_explicit_timeout_overrides_ctor_default(self, hass):
        """An explicit timeout argument is used instead of the ctor value.

        Validates: SanitizedPresenceBinarySensor relies on this to apply
        the live `departure_delay` per pulse rather than at construction.
        Code: custom_components/sanitized_presence/auto_reset.py::AutoResetBinarySensor.pulse
        Assertion: the delay passed to async_call_later equals the
            explicit timeout, not the ctor value.
        Method:
        1. Arrange: _Probe(reset_timeout=15).
        2. Act: pulse(timeout=60).
        3. Assert: async_call_later was called with delay 60.
        """
        with patch(
            "custom_components.sanitized_presence.auto_reset.async_call_later",
            return_value=MagicMock(),
        ) as mock_later:
            sensor = _Probe(hass, reset_timeout=15)
            sensor.pulse(timeout=60)

            assert mock_later.call_args[0][1] == 60

    def test_no_timeout_falls_back_to_ctor_default(self, hass):
        """pulse() without timeout uses the ctor reset_timeout.

        Validates: backward compatibility with consumers (such as
        door_occupancy) that never pass an explicit timeout.
        Code: custom_components/sanitized_presence/auto_reset.py::AutoResetBinarySensor.pulse
        Assertion: async_call_later delay equals the ctor value.
        Method:
        1. Arrange: _Probe(reset_timeout=15).
        2. Act: pulse().
        3. Assert: async_call_later delay is 15.
        """
        with patch(
            "custom_components.sanitized_presence.auto_reset.async_call_later",
            return_value=MagicMock(),
        ) as mock_later:
            sensor = _Probe(hass, reset_timeout=15)
            sensor.pulse()

            assert mock_later.call_args[0][1] == 15

    def test_pulse_emits_deadline_with_correct_expiry(self, hass):
        """_notify_deadline is invoked with utcnow() + timeout on pulse.

        Validates: companion DeadlineSensorEntity gets the right value.
        A wrong offset would surface a misleading expiry time in the UI
        and in any downstream automation depending on it.
        Code: custom_components/sanitized_presence/auto_reset.py::AutoResetBinarySensor.pulse
        Assertion: deadline_calls contains exactly one entry equal to
            patched_now + timedelta(seconds=timeout).
        Method:
        1. Arrange: patch dt_util.utcnow to a fixed UTC datetime.
        2. Act: pulse(timeout=30).
        3. Assert: deadline_calls == [patched_now + 30s].
        """
        now = datetime(2026, 5, 24, 12, 0, 0, tzinfo=timezone.utc)
        with (
            patch(
                "custom_components.sanitized_presence.auto_reset.async_call_later",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.sanitized_presence.auto_reset.dt_util.utcnow",
                return_value=now,
            ),
        ):
            sensor = _Probe(hass, reset_timeout=15)
            sensor.pulse(timeout=30)

            assert sensor.deadline_calls == [now + timedelta(seconds=30)]

    def test_reset_emits_deadline_none(self, hass):
        """_notify_deadline(None) on timer expiry clears the displayed deadline.

        Validates: when sliding window expires, DeadlineSensorEntity
        receives None so the UI shows "no active timer" instead of a past
        timestamp.
        Code: custom_components/sanitized_presence/auto_reset.py::AutoResetBinarySensor._on_reset
        Assertion: after invoking the scheduled callback, deadline_calls
            ends with [None].
        Method:
        1. Arrange: capture the callback, drop the pulse() deadline call.
        2. Act: invoke the captured callback.
        3. Assert: deadline_calls == [None].
        """
        captured: dict = {}
        with (
            patch(
                "custom_components.sanitized_presence.auto_reset.async_call_later",
                side_effect=lambda _h, _d, cb: captured.__setitem__("cb", cb) or MagicMock(),
            ),
            patch("custom_components.sanitized_presence.auto_reset.dt_util.utcnow"),
        ):
            sensor = _Probe(hass, reset_timeout=15)
            sensor.pulse()
            sensor.deadline_calls.clear()

            captured["cb"](None)

            assert sensor.deadline_calls == [None]
