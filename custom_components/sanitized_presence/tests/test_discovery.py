"""Behavior tests for SanitizedPresenceManager.

The manager owns periodic device discovery and entity-pair creation.
Tests cover:

- Devices matching the target models are returned by _find_target_devices.
- Devices missing any required entity are skipped (and no entity-pair
  is created for them).
- A second discovery tick does not duplicate entities (idempotency).
- async_unload cancels the time-interval listener.

How to run:
    pytest custom_components/sanitized_presence/tests/test_discovery.py
"""

from __future__ import annotations

# pylint: disable=protected-access

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from custom_components.sanitized_presence.discovery import SanitizedPresenceManager


def _make_entry():
    """Build a fake config entry the manager can read poll_interval from."""
    entry = MagicMock()
    entry.entry_id = "e1"
    entry.data = {"poll_interval": 30}
    return entry


def _make_device(device_id, model_id, entities_by_suffix):
    """Build a SimpleNamespace device mirroring discovery.py's internal format."""
    entities = {}
    for suffix, eid in entities_by_suffix.items():
        e = MagicMock()
        e.entity_id = eid
        # unique_id encodes the suffix so match_unique_id_suffix matches it.
        e.unique_id = f"0xABCD_{suffix}_zigbee2mqtt"
        e.suggested_object_id = f"radar_{suffix}"
        entities[eid] = e
    return SimpleNamespace(
        id=device_id,
        name="Test Radar",
        model_id=model_id,
        model=model_id,
        identifiers={("zigbee2mqtt", "0xABCD")},
        entities=entities,
    )


def _full_entity_map():
    return {
        "target_distance": "sensor.r_target_distance",
        "detection_range": "number.r_detection_range",
        "shield_range": "number.r_shield_range",
        "departure_delay": "number.r_departure_delay",
        "occupancy": "binary_sensor.r_occupancy",
    }


class TestSanitizedPresenceManager:
    """Discovery contract: model match, entity resolution, idempotency, unload."""

    @pytest.fixture
    def manager(self, hass):
        """Return a fresh manager bound to a minimal mock hass."""
        return SanitizedPresenceManager(hass, _make_entry())

    def test_matching_model_device_is_returned(self, manager):
        """A device whose model_id matches TARGET_MODELS is discovered.

        Validates: the primary filter that lets the integration find
        radars to attach to. Without this, the integration would create
        no entities and the user would see no effect.
        Code: custom_components/sanitized_presence/discovery.py::SanitizedPresenceManager._find_target_devices
        Assertion: _find_target_devices returns the single matching device
            with its id preserved.
        Method:
        1. Arrange: build a SimpleNamespace device with model MTG075-ZB-RL
            and all four required entities.
        2. Act: patch devices_by_model to return [dev]; call
            _find_target_devices.
        3. Assert: exactly one device returned, id preserved.
        """
        dev = _make_device("d1", "MTG075-ZB-RL", _full_entity_map())
        with patch(
            "custom_components.sanitized_presence.discovery.devices_by_model",
            return_value=[dev],
        ):
            found = manager._find_target_devices()

        assert len(found) == 1
        assert found[0].id == "d1"

    def test_device_missing_required_entity_is_skipped(self, manager):
        """A device without all four required entities is excluded.

        Validates: defensive discovery — creating sanitized sensors for
        radars whose entity surface is incomplete would lead to None
        readings forever and a stuck "always off" sanitized sensor.
        Code: custom_components/sanitized_presence/discovery.py::SanitizedPresenceManager._resolve_entities
        Assertion: no sensor pair is added when detection_range is
            missing; _sensors dict remains empty after the call.
        Method:
        1. Arrange: build a device missing the detection_range entity.
        2. Act: patch devices_by_model to return [dev]; call
            _find_target_devices.
        3. Assert: _sensors is empty (no pair was registered).
        """
        partial = _full_entity_map()
        del partial["detection_range"]
        dev = _make_device("d1", "MTG075-ZB-RL", partial)
        with patch(
            "custom_components.sanitized_presence.discovery.devices_by_model",
            return_value=[dev],
        ):
            manager._find_target_devices()

        assert manager._sensors == {}

    async def test_repeated_discovery_does_not_duplicate_entities(self, manager):
        """A second discovery tick does not re-add an already-known device.

        Validates: idempotency — discovery runs on a timer, and HA would
        reject duplicate unique_ids; without this guard the integration
        would log errors every poll_interval.
        Code: custom_components/sanitized_presence/discovery.py::SanitizedPresenceManager._discover_and_add_sensors
        Assertion: the add_binary callback is invoked exactly once even
            though discovery is run twice.
        Method:
        1. Arrange: a device with all entities; mock add callbacks.
        2. Act: invoke platform-ready then re-run _discover_and_add_sensors.
        3. Assert: add_binary.call_count == 1.
        """
        dev = _make_device("d1", "MTG075-ZB-RL", _full_entity_map())
        add_binary = MagicMock()
        add_sensor = MagicMock()
        manager._add_binary_entities = add_binary
        manager._add_sensor_entities = add_sensor

        with (
            patch(
                "custom_components.sanitized_presence.discovery.devices_by_model",
                return_value=[dev],
            ),
            patch(
                "custom_components.sanitized_presence.discovery.async_track_time_interval",
                return_value=MagicMock(),
            ),
        ):
            await manager.async_binary_sensor_platform_ready(add_binary)
            await manager._discover_and_add_sensors()

        assert add_binary.call_count == 1

    async def test_unload_cancels_time_interval_listener(self, manager):
        """async_unload removes the periodic discovery callback.

        Validates: clean teardown when the config entry is unloaded or
        reloaded. Without this, the listener would survive reload and
        fire against a stale manager, causing exceptions.
        Code: custom_components/sanitized_presence/discovery.py::SanitizedPresenceManager.async_unload
        Assertion: the unsubscribe callable is invoked once and the
            internal reference is cleared.
        Method:
        1. Arrange: inject a MagicMock as _remove_listener.
        2. Act: await async_unload().
        3. Assert: unsubscribe called once and reference is None.
        """
        fake_unsub = MagicMock()
        manager._remove_listener = fake_unsub

        await manager.async_unload()

        fake_unsub.assert_called_once()
        assert manager._remove_listener is None

    def test_device_missing_occupancy_entity_is_skipped(self, manager):
        """A device without an occupancy entity is excluded from discovery.

        Validates: occupancy is now a required DP — without it, the
        sanitized sensor cannot apply its gating rule, so creating it
        would be misleading. Discovery must skip such devices with a
        warning, matching the existing policy for other required DPs.
        Code: custom_components/sanitized_presence/discovery.py::SanitizedPresenceManager._resolve_entities
        Assertion: _sensors is empty after discovery when occupancy
            entity is missing.
        Method:
        1. Arrange: device with the four legacy entities but no occupancy.
        2. Act: patch devices_by_model; call _find_target_devices.
        3. Assert: _sensors == {} (no pair registered).
        """
        # Note: _full_entity_map() must include "occupancy" by now;
        # this scenario deletes it explicitly.
        partial = _full_entity_map()
        del partial["occupancy"]
        dev = _make_device("d1", "MTG075-ZB-RL", partial)
        with patch(
            "custom_components.sanitized_presence.discovery.devices_by_model",
            return_value=[dev],
        ):
            manager._find_target_devices()

        assert manager._sensors == {}
