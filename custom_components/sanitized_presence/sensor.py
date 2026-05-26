"""Sensor platform for Sanitized Presence — hosts DeadlineSensorEntity."""

from __future__ import annotations

import logging
from datetime import datetime

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


class DeadlineSensorEntity(SensorEntity):
    """Read-only sensor showing the sanitized_presence deadline expiry time."""

    _attr_should_poll = False
    _attr_icon = "mdi:timer-outline"
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
        self._attr_unique_id = f"{device_id}_sanitized_presence_deadline"
        self._attr_name = f"{device_name} Sanitized Presence Deadline"
        self._attr_native_value: str | None = None

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers=self._device_identifiers)

    def set_expiry(self, expiry_dt: datetime | None) -> None:
        """Called by the binary sensor when the deadline changes."""
        if expiry_dt is None:
            self._attr_native_value = None
        else:
            self._attr_native_value = expiry_dt.isoformat()
        if self.entity_id is not None:
            self.async_write_ha_state()
