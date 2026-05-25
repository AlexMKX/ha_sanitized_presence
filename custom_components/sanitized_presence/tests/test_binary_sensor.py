"""Behavior tests for SanitizedPresenceBinarySensor.

The class is the single decision point of the integration. Tests are
organized into four groups, each validating an independent contract:

- TestEvaluatePulseDecision: when _evaluate calls pulse() vs no-ops.
- TestEvaluateAttributes: extra_state_attributes reflect the latest eval.
- TestTargetEventHandling: state_changed events drive _evaluate.
- TestTickScheduling: tick interval derived from live departure_delay.
- TestDeadlineFanout: _notify_deadline propagates to the companion sensor.

How to run:
    pytest custom_components/sanitized_presence/tests/test_binary_sensor.py
"""

from __future__ import annotations

# pylint: disable=protected-access

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from homeassistant.components.binary_sensor import BinarySensorDeviceClass

from custom_components.sanitized_presence.binary_sensor import SanitizedPresenceBinarySensor
from custom_components.sanitized_presence.const import DEFAULT_DELAY_S


def _make_state(value: str):
    s = MagicMock()
    s.state = value
    return s


def _make_sensor(hass, *, target_eid="sensor.radar_target_distance"):
    entry = MagicMock()
    entry.entry_id = "e1"
    return SanitizedPresenceBinarySensor(
        hass=hass,
        entry=entry,
        device_id="dev1",
        device_name="Radar 1",
        device_identifiers={("zigbee2mqtt", "0xABCD")},
        target_distance_eid=target_eid,
        detection_range_eid="number.radar_detection_range",
        shield_range_eid="number.radar_shield_range",
        departure_delay_eid="number.radar_departure_delay",
    )


def _setup_hass_states(hass, target, detect, shield, delay):
    """Wire hass.states.get(...) to return mocked states for the four eids."""

    def _state_or_unavailable(value):
        return _make_state(str(value)) if value is not None else _make_state("unavailable")

    mapping = {
        "sensor.radar_target_distance": _state_or_unavailable(target),
        "number.radar_detection_range": _state_or_unavailable(detect),
        "number.radar_shield_range": _state_or_unavailable(shield),
        "number.radar_departure_delay": _state_or_unavailable(delay),
    }
    hass.states.get.side_effect = mapping.get


class TestEvaluatePulseDecision:
    """_evaluate either calls pulse(timeout=...) or does nothing."""

    def test_in_range_target_pulses_with_clamped_delay(self, hass):
        """A target inside the window pulses with the live (clamped) delay.

        Validates: the happy path — radar reports a target within
        (shield_floor, detection_range), so the sliding-window timer
        gets armed with the radar's own departure_delay value. Also
        asserts the identity sanity checks (unique_id, device_class,
        initial is_on) so an accidental rename or class change fails
        loudly here instead of breaking HA wiring silently.
        Code: custom_components/sanitized_presence/binary_sensor.py::SanitizedPresenceBinarySensor._evaluate
        Assertion: pulse is called exactly once with timeout=30.0;
            identity attrs match the contract; initial is_on is False.
        Method:
        1. Arrange: mocked hass with target=1.5, detect=4.5, shield=0, delay=30.
        2. Act: patch sensor.pulse, call _evaluate("test").
        3. Assert: identity sanity + pulse called once with timeout=30.0.
        """
        sensor = _make_sensor(hass)
        _setup_hass_states(hass, target=1.5, detect=4.5, shield=0.0, delay=30)

        with patch.object(sensor, "pulse") as mock_pulse:
            sensor._evaluate("test")

        # Identity sanity — a silent rename/class swap would break HA wiring.
        assert sensor.unique_id == "dev1_sanitized_presence"
        assert sensor.device_class == BinarySensorDeviceClass.OCCUPANCY
        assert sensor.is_on is False  # pulse is mocked so state did not flip
        # Behavior under test.
        mock_pulse.assert_called_once_with(timeout=30.0)

    @pytest.mark.parametrize(
        ("scenario", "target", "detect", "shield", "delay"),
        [
            ("target_zero", 0.0, 4.5, 0.0, 30),  # no target
            ("target_above_detect", 5.0, 4.5, 0.0, 30),
            ("target_unavailable", None, 4.5, 0.0, 30),
            ("detect_unavailable", 1.5, None, 0.0, 30),
        ],
    )
    def test_out_of_range_or_missing_data_does_not_pulse(
        self, hass, scenario, target, detect, shield, delay
    ):
        """No pulse when the target is out of range or critical data missing.

        Validates: the integration only ever turns the sensor on when
        confident there is a real target inside the radar's configured
        window. Missing detection_range or target_distance must be a
        no-op (the running deadline, if any, expires naturally).
        Code: custom_components/sanitized_presence/binary_sensor.py::SanitizedPresenceBinarySensor._evaluate
        Assertion: pulse is not called for any of the four scenarios.
        Method:
        1. Arrange: parametrize four edge-case state combinations.
        2. Act: patch pulse, call _evaluate.
        3. Assert: pulse was never invoked.
        """
        sensor = _make_sensor(hass)
        _setup_hass_states(hass, target=target, detect=detect, shield=shield, delay=delay)

        with patch.object(sensor, "pulse") as mock_pulse:
            sensor._evaluate(scenario)

        mock_pulse.assert_not_called()

    def test_missing_departure_delay_falls_back_to_default(self, hass):
        """Pulse uses DEFAULT_DELAY_S when the device's delay entity is unavailable.

        Validates: the integration still applies a sane sliding window
        even when the radar's departure_delay entity briefly disappears
        during HA startup or after a Z2M restart.
        Code: custom_components/sanitized_presence/binary_sensor.py::SanitizedPresenceBinarySensor._evaluate
        Assertion: pulse called with timeout == DEFAULT_DELAY_S.
        Method:
        1. Arrange: hass with target in range and delay = None.
        2. Act: call _evaluate.
        3. Assert: pulse called with DEFAULT_DELAY_S.
        """
        sensor = _make_sensor(hass)
        _setup_hass_states(hass, target=1.5, detect=4.5, shield=0.0, delay=None)

        with patch.object(sensor, "pulse") as mock_pulse:
            sensor._evaluate("test")

        mock_pulse.assert_called_once_with(timeout=float(DEFAULT_DELAY_S))

    @pytest.mark.parametrize(
        ("raw_delay", "expected_timeout"),
        [
            (1, 10.0),  # below DELAY_MIN_S=10
            (9999, 600.0),  # above DELAY_MAX_S=600
            (45, 45.0),  # within bounds, used verbatim
        ],
    )
    def test_pulse_timeout_is_clamped_to_safe_bounds(self, hass, raw_delay, expected_timeout):
        """Out-of-spec radar departure_delay values are clamped.

        Validates: the integration never schedules a meaningless 1s
        deadline (sensor would flap) or a 1h deadline (sensor would
        stick). Critical for robustness against user misconfiguration.
        Code: custom_components/sanitized_presence/binary_sensor.py::SanitizedPresenceBinarySensor._evaluate
        Assertion: pulse timeout equals the clamped value for each case.
        Method:
        1. Arrange: parametrize raw delay across below/within/above bounds.
        2. Act: call _evaluate with target in range.
        3. Assert: pulse called with the expected clamped timeout.
        """
        sensor = _make_sensor(hass)
        _setup_hass_states(hass, target=1.5, detect=4.5, shield=0.0, delay=raw_delay)

        with patch.object(sensor, "pulse") as mock_pulse:
            sensor._evaluate("test")

        mock_pulse.assert_called_once_with(timeout=expected_timeout)


class TestEvaluateAttributes:
    """extra_state_attributes mirror the most recent _evaluate decision."""

    def test_attributes_reflect_latest_evaluation(self, hass):
        """extra_state_attributes expose the effective bounds, timeout, and reason.

        Validates: the diagnostic surface the user inspects in HA when
        debugging a stuck or never-firing sensor. If these values are
        stale or wrong, the user has no signal to act on.
        Code: custom_components/sanitized_presence/binary_sensor.py::SanitizedPresenceBinarySensor.extra_state_attributes
        Assertion: after _evaluate("tick"), attributes contain the
            effective_min (shield_floor here), effective_max (detect),
            effective_timeout (delay), the source entity ids, and the
            last_eval_reason.
        Method:
        1. Arrange: target in range, delay=30; mock pulse so no real
            scheduling happens.
        2. Act: call _evaluate("tick").
        3. Assert: each expected attribute equals its computed value.
        """
        sensor = _make_sensor(hass)
        _setup_hass_states(hass, target=1.5, detect=4.5, shield=0.0, delay=30)

        with patch.object(sensor, "pulse"):
            sensor._evaluate("tick")

        attrs = sensor.extra_state_attributes
        assert attrs["last_eval_reason"] == "tick"
        assert attrs["effective_min"] == pytest.approx(0.1)
        assert attrs["effective_max"] == pytest.approx(4.5)
        assert attrs["effective_timeout"] == pytest.approx(30.0)
        assert attrs["target_distance_eid"] == "sensor.radar_target_distance"


class TestTargetEventHandling:
    """state_changed events on target_distance route into _evaluate."""

    def test_numeric_state_change_triggers_evaluate(self, hass):
        """A numeric target_distance change calls _evaluate("target_change").

        Validates: the integration reacts in real time to the radar's
        measurement updates instead of waiting for the periodic tick.
        Code: custom_components/sanitized_presence/binary_sensor.py::SanitizedPresenceBinarySensor._handle_target_event
        Assertion: _evaluate is called exactly once with reason
            "target_change".
        Method:
        1. Arrange: build an event with numeric new_state ("2.0").
        2. Act: patch _evaluate, call _handle_target_event(event).
        3. Assert: _evaluate called once with "target_change".
        """
        sensor = _make_sensor(hass)
        _setup_hass_states(hass, target=2.0, detect=4.5, shield=0.0, delay=30)
        event = MagicMock()
        event.data = {
            "new_state": _make_state("2.0"),
            "old_state": _make_state("1.9"),
        }
        with patch.object(sensor, "_evaluate") as mock_eval:
            sensor._handle_target_event(event)

        mock_eval.assert_called_once_with("target_change")

    @pytest.mark.parametrize("sentinel", ["unknown", "unavailable"])
    def test_sentinel_state_changes_are_ignored(self, hass, sentinel):
        """Transitions into unknown/unavailable do not invoke _evaluate.

        Validates: the integration does not re-evaluate on signals it
        cannot use; the running deadline (if any) is left to expire on
        its own, matching the documented edge-case behavior.
        Code: custom_components/sanitized_presence/binary_sensor.py::SanitizedPresenceBinarySensor._handle_target_event
        Assertion: _evaluate is never called for either sentinel.
        Method:
        1. Arrange: event with new_state = sentinel.
        2. Act: patch _evaluate, call _handle_target_event.
        3. Assert: _evaluate not called.
        """
        sensor = _make_sensor(hass)
        event = MagicMock()
        event.data = {
            "new_state": _make_state(sentinel),
            "old_state": _make_state("1.5"),
        }
        with patch.object(sensor, "_evaluate") as mock_eval:
            sensor._handle_target_event(event)

        mock_eval.assert_not_called()


class TestTickScheduling:
    """_schedule_tick derives the next tick interval from live departure_delay."""

    def test_tick_interval_is_half_of_departure_delay(self, hass):
        """Tick fires every departure_delay/2 seconds.

        Validates: the documented behavior of "check every half-delay
        whether target is still in range" that adapts live to the user
        changing departure_delay from the radar UI.
        Code: custom_components/sanitized_presence/binary_sensor.py::SanitizedPresenceBinarySensor._schedule_tick
        Assertion: async_call_later is invoked with delay == 30.0 when
            departure_delay == 60.
        Method:
        1. Arrange: departure_delay=60 in hass states.
        2. Act: patch async_call_later, call _schedule_tick.
        3. Assert: delay arg equals 30.0.
        """
        sensor = _make_sensor(hass)
        _setup_hass_states(hass, target=1.5, detect=4.5, shield=0.0, delay=60)

        with patch(
            "custom_components.sanitized_presence.binary_sensor.async_call_later",
            return_value=MagicMock(),
        ) as mock_later:
            sensor._schedule_tick()

        assert mock_later.call_args[0][1] == 30.0

    def test_tick_interval_is_clamped_to_floor(self, hass):
        """An extremely small departure_delay does not produce a runaway tick.

        Validates: TICK_FLOOR_S protects HA from sub-second tick loops
        if a user (or a buggy device) sets departure_delay to its
        minimum.
        Code: custom_components/sanitized_presence/binary_sensor.py::SanitizedPresenceBinarySensor._schedule_tick
        Assertion: when departure_delay=1, the next tick interval equals
            TICK_FLOOR_S (=2.0), not 0.5.
        Method:
        1. Arrange: departure_delay=1.
        2. Act: patch async_call_later, call _schedule_tick.
        3. Assert: delay arg equals 2.0.
        """
        sensor = _make_sensor(hass)
        _setup_hass_states(hass, target=1.5, detect=4.5, shield=0.0, delay=1)

        with patch(
            "custom_components.sanitized_presence.binary_sensor.async_call_later",
            return_value=MagicMock(),
        ) as mock_later:
            sensor._schedule_tick()

        assert mock_later.call_args[0][1] == 2.0


class TestDeadlineFanout:
    """_notify_deadline forwards expiry datetime to the companion sensor."""

    def test_notify_deadline_forwards_to_attached_sensor(self, hass):
        """When a deadline sensor is attached, expiry is forwarded verbatim.

        Validates: the wiring between the binary sensor's sliding-window
        and the user-visible DeadlineSensorEntity on the device card.
        Code: custom_components/sanitized_presence/binary_sensor.py::SanitizedPresenceBinarySensor._notify_deadline
        Assertion: deadline_sensor.update is called once with the same
            datetime instance.
        Method:
        1. Arrange: attach a MagicMock deadline sensor.
        2. Act: call _notify_deadline(dt).
        3. Assert: update called once with dt.
        """
        sensor = _make_sensor(hass)
        deadline_sensor = MagicMock()
        sensor.set_deadline_sensor(deadline_sensor)
        dt = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)

        sensor._notify_deadline(dt)

        deadline_sensor.update.assert_called_once_with(dt)

    def test_notify_deadline_without_attached_sensor_is_a_noop(self, hass):
        """Pre-wiring _notify_deadline calls (during construction) do not raise.

        Validates: the manager constructs the binary sensor before
        attaching the deadline sensor, so the very first pulse during
        async_added_to_hass would call _notify_deadline with no target.
        That must be safe.
        Code: custom_components/sanitized_presence/binary_sensor.py::SanitizedPresenceBinarySensor._notify_deadline
        Assertion: calling _notify_deadline(None) on a sensor without
            attached deadline_sensor does not raise.
        Method:
        1. Arrange: sensor without set_deadline_sensor call.
        2. Act: call _notify_deadline(None).
        3. Assert: no exception (implicit).
        """
        sensor = _make_sensor(hass)
        sensor._notify_deadline(None)
