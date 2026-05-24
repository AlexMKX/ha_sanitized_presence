"""Binary sensor entity: SanitizedPresenceBinarySensor.

One per discovered MTG075/MTG275 radar device. Subscribes to the device's
target_distance state changes and runs a periodic tick to maintain the
sliding-window deadline.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry, entity_registry
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later, async_track_state_change_event

from .auto_reset import AutoResetBinarySensor
from .const import (
    DEFAULT_DELAY_S,
    DELAY_MAX_S,
    DELAY_MIN_S,
    DOMAIN,
    SHIELD_FLOOR_M,
    TICK_CEILING_S,
    TICK_FLOOR_S,
)
from .helpers import _clamp, _to_float, in_range

_LOGGER = logging.getLogger(__name__)

_IGNORED_STATES = {"unknown", "unavailable"}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Hand the binary_sensor platform callback to the manager."""
    manager = hass.data[DOMAIN][entry.entry_id]
    await manager.async_binary_sensor_platform_ready(async_add_entities)


class SanitizedPresenceBinarySensor(AutoResetBinarySensor):
    """Presence sensor derived from target_distance, not the latching presence DP."""

    _attr_device_class = BinarySensorDeviceClass.OCCUPANCY
    _attr_icon = "mdi:motion-sensor"
    _attr_should_poll = False

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        device_id: str,
        device_name: str,
        device_identifiers: set,
        target_distance_eid: str,
        detection_range_eid: str,
        shield_range_eid: str,
        departure_delay_eid: str,
    ) -> None:
        super().__init__(hass, reset_timeout=DEFAULT_DELAY_S)
        self._entry = entry
        self._device_id = device_id
        self._device_identifiers = device_identifiers
        self._target_distance_eid = target_distance_eid
        self._detection_range_eid = detection_range_eid
        self._shield_range_eid = shield_range_eid
        self._departure_delay_eid = departure_delay_eid
        self._attr_name = f"{device_name} Sanitized Presence"
        self._attr_unique_id = f"{device_id}_sanitized_presence"
        self._unsub_state: Callable[[], None] | None = None
        self._cancel_tick: Callable[[], None] | None = None
        self._deadline_sensor = None
        # Attributes updated on every _evaluate call
        self._last_eval_reason: str = "startup"
        self._effective_min: float | None = None
        self._effective_max: float | None = None
        self._effective_timeout: float | None = None

    def set_deadline_sensor(self, deadline_sensor) -> None:
        """Inject the companion deadline sensor (called by discovery manager)."""
        self._deadline_sensor = deadline_sensor

    def _notify_deadline(self, expiry_dt: datetime | None) -> None:
        if self._deadline_sensor is not None:
            self._deadline_sensor.update(expiry_dt)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Bind config entry to the source device so entity shows on device card.
        dev_reg = device_registry.async_get(self.hass)
        dev_reg.async_update_device(
            self._device_id,
            add_config_entry_id=self._entry.entry_id,
        )
        self._unsub_state = async_track_state_change_event(
            self.hass,
            [self._target_distance_eid],
            self._handle_target_event,
        )
        self._schedule_tick()
        self._evaluate("startup")
        _LOGGER.info("SanitizedPresenceBinarySensor created for device_id=%s", self._device_id)

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_state is not None:
            self._unsub_state()
            self._unsub_state = None
        if self._cancel_tick is not None:
            self._cancel_tick()
            self._cancel_tick = None
        await super().async_will_remove_from_hass()

    @callback
    def _handle_target_event(self, event) -> None:
        new_state = event.data.get("new_state")
        if new_state is None or new_state.state in _IGNORED_STATES:
            return
        self._evaluate("target_change")

    @callback
    def _on_tick(self, _now) -> None:
        self._cancel_tick = None
        self._evaluate("tick")
        self._schedule_tick()

    def _schedule_tick(self) -> None:
        state = self.hass.states.get(self._departure_delay_eid)
        raw_delay = _to_float(state.state if state else None)
        base = (raw_delay if raw_delay is not None else DEFAULT_DELAY_S) / 2.0
        interval = _clamp(base, TICK_FLOOR_S, TICK_CEILING_S)
        self._cancel_tick = async_call_later(self.hass, interval, self._on_tick)

    @callback
    def _evaluate(self, reason: str) -> None:
        """Single decision point: pulse if in range, else do nothing."""

        def _read(eid: str) -> float | None:
            s = self.hass.states.get(eid)
            return _to_float(s.state if s else None)

        target = _read(self._target_distance_eid)
        shield = _read(self._shield_range_eid)
        detect = _read(self._detection_range_eid)
        delay = _read(self._departure_delay_eid)

        self._last_eval_reason = reason

        if detect is None or target is None:
            _LOGGER.debug("_evaluate(%s): skip — target=%s detect=%s", reason, target, detect)
            return

        effective_min = max(shield if shield is not None else 0.0, SHIELD_FLOOR_M)
        effective_max = detect
        self._effective_min = effective_min
        self._effective_max = effective_max

        if not in_range(target, shield if shield is not None else 0.0, detect, SHIELD_FLOOR_M):
            _LOGGER.debug(
                "_evaluate(%s): out_of_range target=%s in (%s, %s)",
                reason,
                target,
                effective_min,
                effective_max,
            )
            return

        timeout = _clamp(delay if delay is not None else DEFAULT_DELAY_S, DELAY_MIN_S, DELAY_MAX_S)
        self._effective_timeout = timeout
        _LOGGER.debug(
            "_evaluate(%s): in_range target=%s, pulse timeout=%s", reason, target, timeout
        )
        self.pulse(timeout=timeout)

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers=self._device_identifiers)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "device_id": self._device_id,
            "target_distance_eid": self._target_distance_eid,
            "detection_range_eid": self._detection_range_eid,
            "shield_range_eid": self._shield_range_eid,
            "departure_delay_eid": self._departure_delay_eid,
            "effective_min": self._effective_min,
            "effective_max": self._effective_max,
            "effective_timeout": self._effective_timeout,
            "last_eval_reason": self._last_eval_reason,
        }
