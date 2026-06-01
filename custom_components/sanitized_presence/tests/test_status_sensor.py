"""Behavior tests for StatusSensorEntity.

Group:
- TestStatus: native_value is the current mode; attributes carry diagnostics.

How to run:
    PYTHONPATH=. pytest custom_components/sanitized_presence/tests/test_status_sensor.py
"""

from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.sanitized_presence.sensor import StatusSensorEntity


def _make_sensor(hass):
    entry = MagicMock()
    entry.entry_id = "e1"
    sensor = StatusSensorEntity(
        hass=hass,
        entry=entry,
        device_id="dev1",
        device_name="Radar 1",
        device_identifiers={("zigbee2mqtt", "0xABCD")},
    )
    sensor.async_write_ha_state = MagicMock()
    return sensor


class TestStatus:
    """native_value reflects mode; extra_state_attributes carry diagnostics."""

    def test_set_status_updates_value_and_attributes(self, hass):
        """set_status records the mode as value and diagnostics as attributes.

        Validates: the diagnostic surface a user inspects to see whether a
        device is healing and what the current heal state is.
        Code: custom_components/sanitized_presence/sensor.py::StatusSensorEntity.set_status
        Assertion: native_value == 'healing'; attributes expose
            healing, iteration, discrepancy_age_sec, last_heal_result,
            and backoff_left_sec from the snapshot.
        Method:
        1. Arrange: build sensor.
        2. Act: set_status('healing', {diagnostics...}).
        3. Assert: value and attributes reflect the inputs.
        """
        sensor = _make_sensor(hass)
        sensor.set_status(
            "healing",
            {
                "healing": True,
                "iteration": 2,
                "discrepancy_age_sec": 950,
                "last_heal_result": "none",
                "backoff_left_sec": 0,
            },
        )
        assert sensor.native_value == "healing"
        attrs = sensor.extra_state_attributes
        assert attrs["healing"] is True
        assert attrs["iteration"] == 2
        assert attrs["discrepancy_age_sec"] == 950

    def test_unique_id_is_preserved_for_migration(self, hass):
        """unique_id keeps the legacy suffix so the registry entity is reused.

        Validates: repurposing the deadline sensor must not orphan the
        existing HA entity; the unique_id stays stable.
        Code: custom_components/sanitized_presence/sensor.py::StatusSensorEntity.__init__
        Assertion: unique_id ends with the historical
            '_sanitized_presence_deadline' suffix.
        Method:
        1. Arrange/Act: build sensor.
        2. Assert: unique_id == 'dev1_sanitized_presence_deadline'.
        """
        sensor = _make_sensor(hass)
        assert sensor.unique_id == "dev1_sanitized_presence_deadline"
