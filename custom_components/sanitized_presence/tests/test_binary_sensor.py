"""Behavior tests for SanitizedPresenceBinarySensor (state machine).

Groups:
- TestNormalMirror: NORMAL output mirrors the native presence DP.
- TestLatchTrigger: continuous presence=on >= RECOVERY_PRESENCE_ON_SEC enters RECOVERY.
- TestRecoveryOutput: RECOVERY output uses in_range only (presence ignored).
- TestRecoveryExit: a real presence=off after the cycle returns to NORMAL.
- TestEchoSuppression: presence transitions during an active cycle are ignored.

How to run:
    PYTHONPATH=. pytest custom_components/sanitized_presence/tests/test_binary_sensor.py
"""

from __future__ import annotations

# pylint: disable=protected-access

from unittest.mock import MagicMock

import pytest

from custom_components.sanitized_presence.binary_sensor import (
    MODE_NORMAL,
    MODE_RECOVERY,
    SanitizedPresenceBinarySensor,
)
from custom_components.sanitized_presence.const import RECOVERY_PRESENCE_ON_SEC


def _make_state(value: str):
    s = MagicMock()
    s.state = value
    return s


def _make_sensor(hass, controller=None):
    entry = MagicMock()
    entry.entry_id = "e1"
    sensor = SanitizedPresenceBinarySensor(
        hass=hass,
        entry=entry,
        device_id="dev1",
        device_name="Radar 1",
        device_identifiers={("zigbee2mqtt", "0xABCD")},
        target_distance_eid="sensor.radar_target_distance",
        detection_range_eid="number.radar_detection_range",
        shield_range_eid="number.radar_shield_range",
        presence_eid="binary_sensor.radar_presence",
        controller=controller or MagicMock(is_resetting=False),
    )
    # Bypass HA entity write during unit tests.
    sensor.async_write_ha_state = MagicMock()
    return sensor


def _states(hass, *, presence="on", target=1.5, detect=4.5, shield=0.0):
    mapping = {
        "binary_sensor.radar_presence": _make_state(presence),
        "sensor.radar_target_distance": _make_state(str(target)),
        "number.radar_detection_range": _make_state(str(detect)),
        "number.radar_shield_range": _make_state(str(shield)),
    }
    hass.states.get.side_effect = mapping.get


class TestNormalMirror:
    """In NORMAL the output equals the native presence DP."""

    @pytest.mark.parametrize(("presence", "expected"), [("on", True), ("off", False)])
    def test_output_mirrors_presence(self, hass, presence, expected):
        """NORMAL output is True iff presence == 'on'.

        Validates: the default behavior — sanitized presence is a faithful
        mirror of the radar's own presence DP until recovery is needed.
        Code: custom_components/sanitized_presence/binary_sensor.py::SanitizedPresenceBinarySensor._compute_output
        Assertion: is_on equals expected for presence on/off.
        Method:
        1. Arrange: NORMAL mode, presence as parametrized.
        2. Act: call _recompute(now=0.0).
        3. Assert: is_on == expected; mode stays NORMAL.
        """
        sensor = _make_sensor(hass)
        _states(hass, presence=presence)
        sensor._recompute(now=0.0)
        assert sensor.is_on is expected
        assert sensor._mode == MODE_NORMAL


class TestLatchTrigger:
    """Continuous presence=on past the threshold enters RECOVERY."""

    @pytest.mark.asyncio
    async def test_presence_on_past_threshold_enters_recovery(self, hass):
        """presence=on held RECOVERY_PRESENCE_ON_SEC enters RECOVERY + resets.

        Validates: the primary latch trigger that detects a radar stuck
        reporting presence and kicks off firmware recovery.
        Code: custom_components/sanitized_presence/binary_sensor.py::SanitizedPresenceBinarySensor._recompute
        Assertion: after the threshold elapses with presence on, mode is
            RECOVERY and controller.request_reset was awaited.
        Method:
        1. Arrange: presence on at t=0 (records on-start); controller mock.
        2. Act: _recompute at t=RECOVERY_PRESENCE_ON_SEC.
        3. Assert: mode RECOVERY; request_reset called.
        """
        controller = MagicMock(is_resetting=False)
        # Plain MagicMock (not a coroutine): _enter_recovery schedules it via
        # hass.async_create_task, so we only need to verify it was called.
        controller.request_reset = MagicMock(return_value=True)
        sensor = _make_sensor(hass, controller)
        _states(hass, presence="on")

        sensor._recompute(now=0.0)  # arm on-start
        sensor._recompute(now=float(RECOVERY_PRESENCE_ON_SEC))

        assert sensor._mode == MODE_RECOVERY
        controller.request_reset.assert_called()

    def test_intervening_off_resets_the_timer(self, hass):
        """A presence=off resets the on-start timer, preventing entry.

        Validates: only *continuous* presence triggers recovery; normal
        on/off activity must not.
        Code: custom_components/sanitized_presence/binary_sensor.py::SanitizedPresenceBinarySensor._recompute
        Assertion: an off at mid-window resets _presence_on_since so a
            later recompute (still before a fresh full window) stays NORMAL.
        Method:
        1. Arrange: presence on at t=0; off at t=500; on again at t=600.
        2. Act: recompute at t=1000 (only 400s continuous).
        3. Assert: mode NORMAL.
        """
        assert RECOVERY_PRESENCE_ON_SEC == 900  # guard: math below assumes this
        sensor = _make_sensor(hass)

        _states(hass, presence="on")
        sensor._recompute(now=0.0)
        _states(hass, presence="off")
        sensor._recompute(now=500.0)
        _states(hass, presence="on")
        sensor._recompute(now=600.0)
        sensor._recompute(now=1000.0)  # only 400s continuous on

        assert sensor._mode == MODE_NORMAL


class TestRecoveryOutput:
    """In RECOVERY the output uses in_range only; presence is ignored."""

    @pytest.mark.parametrize(
        ("target", "expected"),
        [(1.5, True), (5.0, False), (0.0, False)],
    )
    def test_recovery_output_is_in_range(self, hass, target, expected):
        """RECOVERY output reflects in_range(shield<target<detect) only.

        Validates: during recovery the untrusted presence DP is dropped and
        the measured target distance decides the output.
        Code: custom_components/sanitized_presence/binary_sensor.py::SanitizedPresenceBinarySensor._compute_output
        Assertion: with presence forced 'on', output tracks in_range of the
            target, not presence.
        Method:
        1. Arrange: force mode RECOVERY; presence 'on'; vary target.
        2. Act: _recompute.
        3. Assert: is_on == expected.
        """
        controller = MagicMock(is_resetting=True)
        sensor = _make_sensor(hass, controller)
        sensor._mode = MODE_RECOVERY
        _states(hass, presence="on", target=target, detect=4.5, shield=0.0)

        sensor._recompute(now=10.0)

        assert sensor.is_on is expected


class TestRecoveryExit:
    """A real presence=off after the cycle returns to NORMAL."""

    def test_real_off_after_cycle_returns_to_normal(self, hass):
        """presence=off observed when not resetting exits RECOVERY.

        Validates: the recovery exit condition — a genuine clear (not an
        echo of our own cycle) proves the firmware is alive again.
        Code: custom_components/sanitized_presence/binary_sensor.py::SanitizedPresenceBinarySensor._recompute
        Assertion: in RECOVERY, controller not resetting, presence 'off'
            -> mode NORMAL and output False.
        Method:
        1. Arrange: mode RECOVERY; controller.is_resetting False; presence off.
        2. Act: _recompute.
        3. Assert: mode NORMAL; is_on False.
        """
        controller = MagicMock(is_resetting=False)
        sensor = _make_sensor(hass, controller)
        sensor._mode = MODE_RECOVERY
        _states(hass, presence="off")

        sensor._recompute(now=10.0)

        assert sensor._mode == MODE_NORMAL
        assert sensor.is_on is False


class TestEchoSuppression:
    """Presence transitions during an active cycle are ignored."""

    def test_presence_off_during_cycle_does_not_exit(self, hass):
        """An 'off' while controller.is_resetting stays in RECOVERY.

        Validates: the echo-suppression gate — the 'off' phase of our own
        reset cycle must not be mistaken for a real clear.
        Code: custom_components/sanitized_presence/binary_sensor.py::SanitizedPresenceBinarySensor._recompute
        Assertion: in RECOVERY with is_resetting True and presence 'off',
            mode remains RECOVERY.
        Method:
        1. Arrange: mode RECOVERY; controller.is_resetting True; presence off.
        2. Act: _recompute.
        3. Assert: mode stays RECOVERY.
        """
        controller = MagicMock(is_resetting=True)
        sensor = _make_sensor(hass, controller)
        sensor._mode = MODE_RECOVERY
        _states(hass, presence="off")

        sensor._recompute(now=10.0)

        assert sensor._mode == MODE_RECOVERY
