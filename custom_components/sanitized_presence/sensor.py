"""Sensor platform for Sanitized Presence — hosts StatusSensorEntity."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Hand the sensor platform callback to the discovery manager."""
    manager = hass.data[DOMAIN][entry.entry_id]
    await manager.async_sensor_platform_ready(async_add_entities)


# Backward-compatibility alias — discovery.py still imports this name;
# it will be rewired to StatusSensorEntity in Task 8.
DeadlineSensorEntity = None  # type: ignore[assignment]


class StatusSensorEntity(SensorEntity):
    """Diagnostic sensor exposing the current mode and recovery diagnostics."""

    _attr_should_poll = False
    _attr_icon = "mdi:state-machine"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        device_id: str,
        device_name: str,
        device_identifiers: set,
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._device_identifiers = device_identifiers
        # Keep the legacy unique_id so the existing registry entity is reused
        # (the old DeadlineSensorEntity used this suffix); only the role and
        # friendly name change.
        self._attr_unique_id = f"{device_id}_sanitized_presence_deadline"
        self._attr_name = f"{device_name} Sanitized Presence Status"
        self._attr_native_value: str | None = None
        self._diagnostics: dict[str, Any] = {}

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers=self._device_identifiers)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return dict(self._diagnostics)

    def set_status(self, mode: str, diagnostics: dict[str, Any]) -> None:
        """Called by the binary sensor when mode or diagnostics change."""
        self._attr_native_value = mode
        self._diagnostics = diagnostics
        if getattr(self, "entity_id", None) is not None:
            self.async_write_ha_state()
