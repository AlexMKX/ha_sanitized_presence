"""Behavior tests for SanitizedPresenceBinarySensor (discrepancy-driven heal).

How to run:
    PYTHONPATH=. pytest custom_components/sanitized_presence/tests/test_binary_sensor.py
"""

from __future__ import annotations

# pylint: disable=protected-access

from unittest.mock import MagicMock

from custom_components.sanitized_presence.binary_sensor import (
    STATE_BACKOFF,
    STATE_HEALING,
    STATE_NORMAL,
    SanitizedPresenceBinarySensor,
)
from custom_components.sanitized_presence.const import DISCREPANCY_TRIGGER_SEC


def _state(value):
    s = MagicMock()
    s.state = value
    return s


def _make_sensor(hass=None, controller=None):
    hass = hass or MagicMock()
    controller = controller or MagicMock()
    controller.is_healing = False
    controller.diagnostics = MagicMock(return_value={"healing": False, "iteration": 0})
    sensor = SanitizedPresenceBinarySensor(
        hass=hass,
        entry=MagicMock(),
        device_id="dev1",
        device_name="Radar 1",
        device_identifiers={("zigbee2mqtt", "dev1")},
        target_distance_eid="sensor.d",
        detection_range_eid="number.det",
        shield_range_eid="number.sh",
        presence_eid="binary_sensor.p",
        controller=controller,
    )
    return sensor


def _set_states(hass, presence="off", distance=0.0, detect=5.0, shield=0.0):
    table = {
        "binary_sensor.p": _state(presence),
        "sensor.d": _state(distance),
        "number.det": _state(detect),
        "number.sh": _state(shield),
        "select.s": _state("on"),
    }
    hass.states.get = table.get
    return table


class TestNormalMirror:
    """In NORMAL mode, output mirrors the native presence DP verbatim."""

    def test_mirrors_presence_on(self):
        """NORMAL output mirrors presence=on.

        Code: binary_sensor.py::SanitizedPresenceBinarySensor._recompute
        """
        s = _make_sensor()
        _set_states(s.hass, presence="on", distance=2.0)
        s._recompute(now=100.0)
        assert s._attr_is_on is True
        assert s._state == STATE_NORMAL

    def test_mirrors_presence_off(self):
        """NORMAL output mirrors presence=off.

        Code: binary_sensor.py::SanitizedPresenceBinarySensor._recompute
        """
        s = _make_sensor()
        _set_states(s.hass, presence="off")
        s._recompute(now=100.0)
        assert s._attr_is_on is False


class TestDiscrepancyTrigger:
    """Discrepancy timer starts when presence=on and distance is out of range."""

    def test_discrepancy_accumulates_and_triggers(self):
        """presence=on + out-of-range for >= DISCREPANCY_TRIGGER_SEC enters HEALING.

        Code: binary_sensor.py::SanitizedPresenceBinarySensor._recompute / _enter_heal
        """
        s = _make_sensor()
        _set_states(s.hass, presence="on", distance=0.0)  # 0 => not in range
        s._recompute(now=0.0)
        assert s._state == STATE_NORMAL  # just started accumulating
        s._recompute(now=float(DISCREPANCY_TRIGGER_SEC))
        assert s._state == STATE_HEALING
        s.hass.async_create_task.assert_called()

    def test_in_range_clears_discrepancy(self):
        """A distance in range resets the discrepancy timer (no trigger).

        Code: binary_sensor.py::SanitizedPresenceBinarySensor._recompute
        """
        s = _make_sensor()
        _set_states(s.hass, presence="on", distance=0.0)
        s._recompute(now=0.0)
        _set_states(s.hass, presence="on", distance=2.0)  # in range
        s._recompute(now=10.0)
        _set_states(s.hass, presence="on", distance=0.0)
        s._recompute(now=float(DISCREPANCY_TRIGGER_SEC))  # only ~T-10 of new accumulation
        assert s._state == STATE_NORMAL

    def test_presence_off_clears_discrepancy(self):
        """presence going off resets the discrepancy timer.

        Code: binary_sensor.py::SanitizedPresenceBinarySensor._recompute
        """
        s = _make_sensor()
        _set_states(s.hass, presence="on", distance=0.0)
        s._recompute(now=0.0)
        _set_states(s.hass, presence="off", distance=0.0)
        s._recompute(now=10.0)
        _set_states(s.hass, presence="on", distance=0.0)
        s._recompute(now=float(DISCREPANCY_TRIGGER_SEC))
        assert s._state == STATE_NORMAL


class TestFreezeDuringHeal:
    """While HEALING, output is frozen regardless of presence/distance changes."""

    def test_output_frozen_while_healing(self):
        """While HEALING, output does not change with presence/distance.

        Code: binary_sensor.py::SanitizedPresenceBinarySensor._recompute
        """
        s = _make_sensor()
        s._state = STATE_HEALING
        s._attr_is_on = True
        _set_states(s.hass, presence="off", distance=0.0)
        s._recompute(now=100.0)
        assert s._attr_is_on is True  # frozen


class TestApplyHealResult:
    """_apply_heal_result transitions state and output based on the heal outcome."""

    def test_occupied_sets_on_and_normal(self):
        """Heal result 'occupied' -> output on, state NORMAL, discrepancy cleared.

        Code: binary_sensor.py::SanitizedPresenceBinarySensor._apply_heal_result
        """
        s = _make_sensor()
        s._state = STATE_HEALING
        s._apply_heal_result("occupied", now=100.0)
        assert s._attr_is_on is True
        assert s._state == STATE_NORMAL
        assert s._discrepancy_since is None

    def test_empty_sets_off_and_normal(self):
        """Heal result 'empty' -> output off, state NORMAL.

        Code: binary_sensor.py::SanitizedPresenceBinarySensor._apply_heal_result
        """
        s = _make_sensor()
        s._state = STATE_HEALING
        s._attr_is_on = True
        s._apply_heal_result("empty", now=100.0)
        assert s._attr_is_on is False
        assert s._state == STATE_NORMAL

    def test_gave_up_enters_backoff(self):
        """Heal result 'gave_up' -> state BACKOFF with a future backoff_until.

        Code: binary_sensor.py::SanitizedPresenceBinarySensor._apply_heal_result
        """
        s = _make_sensor()
        s._state = STATE_HEALING
        s._apply_heal_result("gave_up", now=100.0)
        assert s._state == STATE_BACKOFF
        assert s._backoff_until > 100.0


class TestBackoffGate:
    """Backoff gate suppresses heal re-entry until the backoff timer expires."""

    def test_no_heal_during_backoff(self):
        """Discrepancy met but inside backoff -> stays out of HEALING.

        Code: binary_sensor.py::SanitizedPresenceBinarySensor._recompute
        """
        s = _make_sensor()
        s._state = STATE_BACKOFF
        s._backoff_until = 10_000.0
        s._discrepancy_since = 0.0
        _set_states(s.hass, presence="on", distance=0.0)
        s._recompute(now=float(DISCREPANCY_TRIGGER_SEC))
        assert s._state == STATE_BACKOFF
        s.hass.async_create_task.assert_not_called()

    def test_reenters_heal_after_backoff(self):
        """After backoff expiry with persistent discrepancy -> HEALING.

        Code: binary_sensor.py::SanitizedPresenceBinarySensor._recompute
        """
        s = _make_sensor()
        s._state = STATE_BACKOFF
        s._backoff_until = 50.0
        s._discrepancy_since = 0.0
        _set_states(s.hass, presence="on", distance=0.0)
        s._recompute(now=float(DISCREPANCY_TRIGGER_SEC))  # > backoff_until
        assert s._state == STATE_HEALING
