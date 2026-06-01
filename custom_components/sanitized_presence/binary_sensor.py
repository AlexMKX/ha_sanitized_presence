"""Binary sensor: SanitizedPresenceBinarySensor (discrepancy-driven heal).

One per discovered MTG075/MTG275 radar. Output is the native presence DP,
except while HEALING when it is frozen.

* NORMAL  — output mirrors native presence verbatim; tracks the discrepancy
            (presence=on while distance out of range).
* HEALING — output frozen at the pre-heal value while the HealController
            runs a confirmed toggle + observe loop.
* BACKOFF — output mirrors native presence; heal re-entry suppressed until
            the backoff timer expires.

distance is used only for the discrepancy trigger and the heal stop
conditions — never as the output value.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)

from .const import (
    DISCREPANCY_TRIGGER_SEC,
    DOMAIN,
    HEAL_BACKOFF_SEC,
    PARK_FALLBACK_INTERVAL_SEC,
    SHIELD_FLOOR_M,
)
from .heal import HealController
from .helpers import _to_float, in_range

_LOGGER = logging.getLogger(__name__)

_IGNORED_STATES = {"unknown", "unavailable"}

STATE_NORMAL = "normal"
STATE_HEALING = "healing"
STATE_BACKOFF = "backoff"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Hand the binary_sensor platform callback to the manager."""
    manager = hass.data[DOMAIN][entry.entry_id]
    await manager.async_binary_sensor_platform_ready(async_add_entities)


class SanitizedPresenceBinarySensor(BinarySensorEntity):
    """Presence sensor that mirrors presence and heals a stuck radar."""

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
        presence_eid: str,
        controller: HealController,
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._device_id = device_id
        self._device_identifiers = device_identifiers
        self._target_distance_eid = target_distance_eid
        self._detection_range_eid = detection_range_eid
        self._shield_range_eid = shield_range_eid
        self._presence_eid = presence_eid
        self._controller = controller
        self._controller.bind_observers(
            presence_on=self._read_presence_on,
            in_range_now=self._read_in_range,
        )
        self._attr_name = f"{device_name} Sanitized Presence"
        self._attr_unique_id = f"{device_id}_sanitized_presence"
        self._attr_is_on = False
        self._state = STATE_NORMAL
        self._discrepancy_since: float | None = None
        self._backoff_until: float = 0.0
        self._last_heal_result: str = "none"
        self._presence_state: str | None = None
        self._status_sensor = None
        self._unsub_state: Callable[[], None] | None = None
        self._unsub_fallback: Callable[[], None] | None = None
        self._heal_task: asyncio.Task | None = None
        self._fallback_task: asyncio.Task | None = None

    def set_status_sensor(self, status_sensor) -> None:
        """Inject the companion status sensor (called by discovery manager)."""
        self._status_sensor = status_sensor

    def _notify_status(self) -> None:
        if self._status_sensor is None:
            return
        now = time.time()
        age = 0 if self._discrepancy_since is None else int(now - self._discrepancy_since)
        backoff_left = max(0, int(self._backoff_until - now))
        diag = {
            "discrepancy_age_sec": age,
            "last_heal_result": self._last_heal_result,
            "backoff_left_sec": backoff_left,
            **self._controller.diagnostics(),
        }
        self._status_sensor.set_status(self._state, diag)

    async def async_added_to_hass(self) -> None:
        dev_reg = device_registry.async_get(self.hass)
        dev_reg.async_update_device(self._device_id, add_config_entry_id=self._entry.entry_id)
        self._unsub_state = async_track_state_change_event(
            self.hass,
            [self._target_distance_eid, self._presence_eid],
            self._handle_source_event,
        )
        self._unsub_fallback = async_track_time_interval(
            self.hass, self._on_fallback_tick, timedelta(seconds=PARK_FALLBACK_INTERVAL_SEC)
        )
        self._recompute(now=time.time())
        _LOGGER.info("SanitizedPresenceBinarySensor created for device_id=%s", self._device_id)

    async def async_will_remove_from_hass(self) -> None:
        for unsub_attr in ("_unsub_state", "_unsub_fallback"):
            unsub = getattr(self, unsub_attr)
            if unsub is not None:
                unsub()
                setattr(self, unsub_attr, None)
        for task_attr in ("_heal_task", "_fallback_task"):
            task = getattr(self, task_attr)
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            setattr(self, task_attr, None)

    # --- readers injected into the controller -------------------------------

    def _read(self, eid: str) -> float | None:
        s = self.hass.states.get(eid)
        return _to_float(s.state if s else None)

    def _read_presence_on(self) -> bool:
        s = self.hass.states.get(self._presence_eid)
        return s is not None and s.state == "on"

    def _read_in_range(self) -> bool:
        target = self._read(self._target_distance_eid)
        detect = self._read(self._detection_range_eid)
        if target is None or detect is None:
            return False
        shield = self._read(self._shield_range_eid) or 0.0
        return in_range(target, shield, detect, SHIELD_FLOOR_M)

    # --- events / ticks ------------------------------------------------------

    @callback
    def _handle_source_event(self, event) -> None:
        new_state = event.data.get("new_state")
        if new_state is None or new_state.state in _IGNORED_STATES:
            return
        self._recompute(now=time.time())

    @callback
    def _on_fallback_tick(self, _now) -> None:
        if self._fallback_task is None or self._fallback_task.done():
            self._fallback_task = self.hass.async_create_task(
                self._controller.maybe_recover_parked()
            )
        # Re-evaluate the state machine each tick so a stuck radar emitting no
        # state events still crosses the discrepancy threshold (and backoff
        # expiry re-enters heal) within PARK_FALLBACK_INTERVAL_SEC.
        self._recompute(now=time.time())

    # --- core decision -------------------------------------------------------

    @callback
    def _recompute(self, now: float) -> None:
        """Single decision point: discrepancy tracking, trigger, output."""
        presence_st = self.hass.states.get(self._presence_eid)
        presence = presence_st.state if presence_st is not None else None
        self._presence_state = presence
        presence_on = presence == "on"

        if self._state == STATE_HEALING:
            # Output frozen; the controller drives the toggles. Only refresh
            # diagnostics (phantom presence/distance must not leak out).
            self._notify_status()
            self._write_state()
            return

        # Track the discrepancy: presence=on AND distance out of range.
        discrepant = presence_on and not self._read_in_range()
        if discrepant:
            if self._discrepancy_since is None:
                self._discrepancy_since = now
        else:
            self._discrepancy_since = None

        if self._state == STATE_BACKOFF and now >= self._backoff_until:
            self._state = STATE_NORMAL

        held = (
            self._discrepancy_since is not None
            and (now - self._discrepancy_since) >= DISCREPANCY_TRIGGER_SEC
        )
        if held and now >= self._backoff_until and self._state != STATE_HEALING:
            self._enter_heal(now)
            return

        # NORMAL / BACKOFF both mirror presence.
        self._attr_is_on = presence_on
        self._notify_status()
        self._write_state()

    def _enter_heal(self, now: float) -> None:
        self._state = STATE_HEALING
        _LOGGER.info(
            "sanitized_presence: %s entering HEALING (discrepancy %ds)",
            self._device_id,
            int(now - (self._discrepancy_since or now)),
        )
        # Output is left frozen at its current value (presence was 'on').
        self._notify_status()
        self._write_state()
        if self._heal_task is None or self._heal_task.done():
            self._heal_task = self.hass.async_create_task(self._run_heal())

    async def _run_heal(self) -> None:
        result = await self._controller.async_heal()
        self._apply_heal_result(result, now=time.time())

    def _apply_heal_result(self, result: str, now: float) -> None:
        self._last_heal_result = result
        if result == "occupied":
            self._attr_is_on = True
            self._state = STATE_NORMAL
            self._discrepancy_since = None
        elif result == "empty":
            self._attr_is_on = False
            self._state = STATE_NORMAL
            self._discrepancy_since = None
        else:  # gave_up
            self._attr_is_on = self._read_presence_on()
            self._backoff_until = now + HEAL_BACKOFF_SEC
            self._state = STATE_BACKOFF
        _LOGGER.info(
            "sanitized_presence: %s heal -> %s (state=%s)",
            self._device_id,
            result,
            self._state,
        )
        self._notify_status()
        self._write_state()

    def _write_state(self) -> None:
        if getattr(self, "entity_id", None) is not None:
            self.async_write_ha_state()

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers=self._device_identifiers)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "device_id": self._device_id,
            "state": self._state,
            "presence_eid": self._presence_eid,
            "presence_state": self._presence_state,
            "target_distance_eid": self._target_distance_eid,
            "detection_range_eid": self._detection_range_eid,
            "shield_range_eid": self._shield_range_eid,
        }
