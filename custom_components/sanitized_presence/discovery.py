"""Periodic discovery of MTG075/MTG275 radar devices and entity creation."""

from __future__ import annotations

import logging
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval

from .binary_sensor import SanitizedPresenceBinarySensor
from .const import (
    CONF_POLL_INTERVAL,
    DEFAULT_POLL_S,
    SUFFIX_DEPARTURE_DELAY,
    SUFFIX_DETECTION_RANGE,
    SUFFIX_OCCUPANCY,
    SUFFIX_SHIELD_RANGE,
    SUFFIX_TARGET_DISTANCE,
    TARGET_MODELS,
)
from .sensor import DeadlineSensorEntity

_LOGGER = logging.getLogger(__name__)

_REQUIRED_SUFFIXES = (
    SUFFIX_TARGET_DISTANCE,
    SUFFIX_DETECTION_RANGE,
    SUFFIX_SHIELD_RANGE,
    SUFFIX_DEPARTURE_DELAY,
    SUFFIX_OCCUPANCY,
)

Z2M_UID_SUFFIX = "zigbee2mqtt"


def match_unique_id_suffix(entry: Any, suffix: str) -> bool:
    """Return True if the entity's unique_id contains _{suffix}_zigbee2mqtt."""
    uid = getattr(entry, "unique_id", None) or ""
    if f"_{suffix}_{Z2M_UID_SUFFIX}" in uid:
        return True
    soid = getattr(entry, "suggested_object_id", None) or ""
    return soid.endswith(f"_{suffix}")


def devices_by_model(model_name: str, hass: HomeAssistant):
    """Return SimpleNamespace devices matching model_name from HA registries."""
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)

    entities_by_device: dict[str, dict] = {}
    for entry in ent_reg.entities.values():
        if entry.device_id:
            entities_by_device.setdefault(entry.device_id, {})[entry.entity_id] = entry

    result = []
    for device in dev_reg.devices.values():
        if (
            getattr(device, "model_id", None) != model_name
            and getattr(device, "model", None) != model_name
        ):
            continue
        result.append(
            SimpleNamespace(
                id=device.id,
                name=getattr(device, "name", None) or model_name,
                model_id=getattr(device, "model_id", None),
                model=getattr(device, "model", None),
                identifiers=set(device.identifiers),
                entities=entities_by_device.get(device.id, {}),
            )
        )
    return result


class SanitizedPresenceManager:
    """Owns per-entry discovery state for Sanitized Presence."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        # device_id → (SanitizedPresenceBinarySensor, DeadlineSensorEntity)
        self._sensors: dict[str, tuple] = {}
        self._add_binary_entities: AddEntitiesCallback | None = None
        self._add_sensor_entities: AddEntitiesCallback | None = None
        self._remove_listener = None
        self._discovery_started = False

    def _find_target_devices(self) -> list:
        """Return list of radar devices with all required entities resolved."""
        seen: set[str] = set()
        result = []
        for model in TARGET_MODELS:
            for device in devices_by_model(model, self.hass):
                if device.id in seen:
                    continue
                seen.add(device.id)
                eids = self._resolve_entities(device)
                if eids is None:
                    continue
                device.eids = eids
                result.append(device)
        return result

    def _resolve_entities(self, device) -> dict[str, str] | None:
        """Map each required suffix to an entity_id; return None if any missing."""
        eids: dict[str, str] = {}
        for suffix in _REQUIRED_SUFFIXES:
            matches = sorted(
                e.entity_id for e in device.entities.values() if match_unique_id_suffix(e, suffix)
            )
            if not matches:
                _LOGGER.warning(
                    "Sanitized Presence: device %s skipped — missing entity with suffix '%s'",
                    device.name,
                    suffix,
                )
                return None
            if len(matches) > 1:
                _LOGGER.warning(
                    "Sanitized Presence: device %s — multiple entities for suffix '%s', using %s",
                    device.name,
                    suffix,
                    matches[0],
                )
            eids[suffix] = matches[0]
        return eids

    async def async_binary_sensor_platform_ready(
        self, async_add_entities: AddEntitiesCallback
    ) -> None:
        """Called by binary_sensor platform setup."""
        self._add_binary_entities = async_add_entities
        await self._maybe_start_discovery()

    async def async_sensor_platform_ready(self, async_add_entities: AddEntitiesCallback) -> None:
        """Called by sensor platform setup."""
        self._add_sensor_entities = async_add_entities
        await self._maybe_start_discovery()

    async def _maybe_start_discovery(self) -> None:
        """Run first discovery and start the poll timer once both platforms are registered.

        Called by both platform-ready callbacks; does nothing until both
        _add_binary_entities and _add_sensor_entities are set, then runs
        exactly once (guarded by _discovery_started).
        """
        if self._add_binary_entities is None or self._add_sensor_entities is None:
            return
        if self._discovery_started:
            return
        self._discovery_started = True
        await self._discover_and_add_sensors()
        poll = self.entry.data.get(CONF_POLL_INTERVAL, DEFAULT_POLL_S)
        self._remove_listener = async_track_time_interval(
            self.hass, self._on_tick, timedelta(seconds=poll)
        )

    async def _on_tick(self, _now) -> None:
        await self._discover_and_add_sensors()

    async def _discover_and_add_sensors(self) -> None:
        if self._add_binary_entities is None:
            return
        devices = self._find_target_devices()
        new_binary = []
        new_sensors = []
        for device in devices:
            if device.id in self._sensors:
                continue
            eids = device.eids
            binary_sensor = SanitizedPresenceBinarySensor(
                hass=self.hass,
                entry=self.entry,
                device_id=device.id,
                device_name=device.name,
                device_identifiers=device.identifiers,
                target_distance_eid=eids[SUFFIX_TARGET_DISTANCE],
                detection_range_eid=eids[SUFFIX_DETECTION_RANGE],
                shield_range_eid=eids[SUFFIX_SHIELD_RANGE],
                departure_delay_eid=eids[SUFFIX_DEPARTURE_DELAY],
                occupancy_eid=eids[SUFFIX_OCCUPANCY],
            )
            deadline_sensor = DeadlineSensorEntity(
                hass=self.hass,
                entry=self.entry,
                device_id=device.id,
                device_name=device.name,
                device_identifiers=device.identifiers,
            )
            binary_sensor.set_deadline_sensor(deadline_sensor)
            self._sensors[device.id] = (binary_sensor, deadline_sensor)
            new_binary.append(binary_sensor)
            new_sensors.append(deadline_sensor)

        if new_binary:
            _LOGGER.info("Sanitized Presence: adding %d new radar sensor pair(s)", len(new_binary))
            self._add_binary_entities(new_binary, update_before_add=True)
        if new_sensors and self._add_sensor_entities is not None:
            self._add_sensor_entities(new_sensors, update_before_add=True)

    async def async_unload(self) -> None:
        """Cancel the periodic discovery listener on config entry unload."""
        if self._remove_listener is not None:
            self._remove_listener()
            self._remove_listener = None
