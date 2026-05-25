"""Behavior tests for DeadlineSensorEntity.

Covers the contract that SanitizedPresenceBinarySensor uses to display
the current sliding-window expiry on the device card:
- update(datetime) sets native_value to its ISO-8601 string and writes
  HA state.
- update(None) clears native_value and writes HA state.

Identity attributes (unique_id, entity_category, icon, should_poll) are
asserted as sanity checks alongside the first behavior test so they
remain low-noise but still fail loudly if accidentally changed.

How to run:
    pytest custom_components/sanitized_presence/tests/test_deadline_sensor.py
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

from homeassistant.const import EntityCategory

from custom_components.sanitized_presence.sensor import DeadlineSensorEntity


def _make_sensor(device_id="dev123", device_name="Radar 1"):
    hass = MagicMock()
    entry = MagicMock()
    entry.entry_id = "entry1"
    return DeadlineSensorEntity(
        hass=hass,
        entry=entry,
        device_id=device_id,
        device_name=device_name,
        device_identifiers={("zigbee2mqtt", "0xAABBCC")},
    )


class TestDeadlineSensorEntity:
    """update() drives native_value transitions; identity stays stable."""

    def test_update_with_datetime_publishes_iso_string(self):
        """update(dt) sets native_value to dt.isoformat() and writes state.

        Validates: the primary behavior consumers see — the deadline
        sensor's native_value reflects the current sliding-window expiry
        in machine-readable ISO-8601 form. Also asserts the identity
        attributes that the integration's UI/group helper depends on so
        an accidental rename or icon swap fails this single behavior test.
        Code: custom_components/sanitized_presence/sensor.py::DeadlineSensorEntity.update
        Assertion: after update(dt), native_value equals dt.isoformat()
            and async_write_ha_state ran exactly once; identity attrs
            (unique_id suffix, entity_category, icon, should_poll) match
            the contract.
        Method:
        1. Arrange: build sensor with device_id="abc"; mock async_write_ha_state.
        2. Act: call update(dt) with a fixed UTC datetime.
        3. Assert: identity sanity (unique_id, category, icon, polling)
            + native_value equals dt.isoformat() + write was called once.
        """
        sensor = _make_sensor(device_id="abc")
        sensor.entity_id = "sensor.radar_1_deadline"
        sensor.async_write_ha_state = MagicMock()
        dt = datetime(2026, 5, 24, 15, 30, 0, tzinfo=timezone.utc)

        sensor.update(dt)

        # Identity sanity: a rename here would break HA wiring silently.
        assert sensor.unique_id == "abc_sanitized_presence_deadline"
        assert sensor.entity_category == EntityCategory.DIAGNOSTIC
        assert sensor.icon == "mdi:timer-outline"
        assert sensor.should_poll is False
        # Behavior under test.
        assert sensor.native_value == dt.isoformat()
        sensor.async_write_ha_state.assert_called_once()

    def test_initial_value_is_none_before_any_update(self):
        """A freshly constructed sensor exposes no deadline.

        Validates: at integration startup the UI shows "no active timer"
        rather than a leftover ISO string from a previous run (the
        integration does not use RestoreEntity).
        Code: custom_components/sanitized_presence/sensor.py::DeadlineSensorEntity.__init__
        Assertion: native_value is None immediately after construction.
        Method:
        1. Arrange: build a fresh sensor.
        2. Act: no operation.
        3. Assert: native_value is None.
        """
        sensor = _make_sensor()
        assert sensor.native_value is None

    def test_update_with_none_clears_previous_value(self):
        """update(None) replaces a previously published ISO string.

        Validates: when the sliding-window expires (AutoResetBinarySensor
        calls _notify_deadline(None)), the deadline sensor must clear its
        value. Otherwise the UI would keep showing a stale past expiry.
        Code: custom_components/sanitized_presence/sensor.py::DeadlineSensorEntity.update
        Assertion: native_value becomes None and async_write_ha_state is
            called once for the clear transition.
        Method:
        1. Arrange: set a non-None value first (update(dt)).
        2. Act: call update(None).
        3. Assert: native_value is None; second write_ha_state observed.
        """
        sensor = _make_sensor()
        sensor.entity_id = "sensor.radar_1_deadline"
        sensor.async_write_ha_state = MagicMock()
        sensor.update(datetime(2026, 5, 24, 15, 30, 0, tzinfo=timezone.utc))
        sensor.async_write_ha_state.reset_mock()

        sensor.update(None)

        assert sensor.native_value is None
        sensor.async_write_ha_state.assert_called_once()
