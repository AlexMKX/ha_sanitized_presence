"""Binary sensor: SanitizedPresenceBinarySensor (recovery state machine).

One per discovered MTG075/MTG275 radar. Two states:

* NORMAL   — output mirrors the native presence DP verbatim.
* RECOVERY — output is in_range(shield < target < detection) only (the
             presence DP is untrusted while recovering), and a firmware
             reset cycle is driven via the injected RecoveryController.

Entry to RECOVERY: presence continuously "on" for RECOVERY_PRESENCE_ON_SEC,
or a periodic health interval elapsing since the last reset. Exit: a real
presence "off" observed while no reset cycle is in flight.
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
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval

from .const import (
    DOMAIN,
    HEALTH_RESET_INTERVAL_SEC,
    OFF_FALLBACK_INTERVAL_SEC,
    RECOVERY_PRESENCE_ON_SEC,
    SHIELD_FLOOR_M,
)
from .helpers import _to_float, in_range
from .recovery import RecoveryController

_LOGGER = logging.getLogger(__name__)

_IGNORED_STATES = {"unknown", "unavailable"}

MODE_NORMAL = "normal"
MODE_RECOVERY = "recovery"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Hand the binary_sensor platform callback to the manager."""
    manager = hass.data[DOMAIN][entry.entry_id]
    await manager.async_binary_sensor_platform_ready(async_add_entities)


class SanitizedPresenceBinarySensor(BinarySensorEntity):
    """Presence sensor that mirrors presence in NORMAL, gates on distance in RECOVERY."""

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
        controller: RecoveryController,
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
        self._attr_name = f"{device_name} Sanitized Presence"
        self._attr_unique_id = f"{device_id}_sanitized_presence"
        self._attr_is_on = False
        self._mode = MODE_NORMAL
        self._presence_on_since: float | None = None
        self._last_reset_anchor: float = time.time()
        self._presence_state: str | None = None
        self._status_sensor = None
        self._unsub_state: Callable[[], None] | None = None
        self._unsub_health: Callable[[], None] | None = None
        self._unsub_fallback: Callable[[], None] | None = None
        self._reset_task: asyncio.Task | None = None
        self._fallback_task: asyncio.Task | None = None

    def set_status_sensor(self, status_sensor) -> None:
        """Inject the companion status sensor (called by discovery manager)."""
        self._status_sensor = status_sensor

    def _notify_status(self) -> None:
        if self._status_sensor is not None:
            self._status_sensor.set_status(self._mode, self._controller.diagnostics())

    async def async_added_to_hass(self) -> None:
        dev_reg = device_registry.async_get(self.hass)
        dev_reg.async_update_device(self._device_id, add_config_entry_id=self._entry.entry_id)
        self._unsub_state = async_track_state_change_event(
            self.hass,
            [self._target_distance_eid, self._presence_eid],
            self._handle_source_event,
        )
        self._unsub_health = async_track_time_interval(
            self.hass, self._on_health_tick, timedelta(seconds=HEALTH_RESET_INTERVAL_SEC)
        )
        self._unsub_fallback = async_track_time_interval(
            self.hass, self._on_fallback_tick, timedelta(seconds=OFF_FALLBACK_INTERVAL_SEC)
        )
        self._recompute(now=time.time())
        _LOGGER.info("SanitizedPresenceBinarySensor created for device_id=%s", self._device_id)

    async def async_will_remove_from_hass(self) -> None:
        for unsub_attr in ("_unsub_state", "_unsub_health", "_unsub_fallback"):
            unsub = getattr(self, unsub_attr)
            if unsub is not None:
                unsub()
                setattr(self, unsub_attr, None)
        # Cancel any in-flight reset / fallback task so a reload mid-cycle
        # does not keep driving the select against a stale entity.
        for task_attr in ("_reset_task", "_fallback_task"):
            task = getattr(self, task_attr)
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            setattr(self, task_attr, None)

    @callback
    def _handle_source_event(self, event) -> None:
        new_state = event.data.get("new_state")
        if new_state is None or new_state.state in _IGNORED_STATES:
            return
        self._recompute(now=time.time())

    @callback
    def _on_health_tick(self, _now) -> None:
        now = time.time()
        if (
            self._mode == MODE_NORMAL
            and (now - self._last_reset_anchor) >= HEALTH_RESET_INTERVAL_SEC
            and (self._reset_task is None or self._reset_task.done())
        ):
            # Silent firmware-freshness select-walk. Does NOT enter RECOVERY:
            # the output keeps mirroring native presence. Only a real latch
            # (presence stuck 'on') changes output semantics.
            self._last_reset_anchor = now
            _LOGGER.info(
                "sanitized_presence: %s health select-walk (background)",
                self._device_id,
            )
            self._reset_task = self.hass.async_create_task(self._controller.request_reset("health"))
        self._recompute(now=now)

    @callback
    def _on_fallback_tick(self, _now) -> None:
        if self._fallback_task is None or self._fallback_task.done():
            self._fallback_task = self.hass.async_create_task(
                self._controller.maybe_recover_parked()
            )
        # Also re-evaluate the state machine each fallback tick (60s cadence)
        # so latch detection on a stuck device that produces no state events
        # is not bottlenecked at the 30-min health interval.
        self._recompute(now=time.time())

    def _read(self, eid: str) -> float | None:
        s = self.hass.states.get(eid)
        return _to_float(s.state if s else None)

    @callback
    def _recompute(self, now: float) -> None:
        """Single decision point: latch tracking, latch eval, mode transitions, output.

        During a reset cycle (controller.is_resetting=True) the radar emits
        phantom presence echoes. To suppress them:
          - _presence_on_since is NOT advanced/reset (frozen tracking)
          - In NORMAL mode, _attr_is_on is NOT recomputed (frozen output)
        BUT the latch check still runs against the frozen _presence_on_since
        so a genuinely stuck native presence is caught at the next tick — the
        eager task factory in HA means is_resetting becomes True synchronously
        when request_reset is scheduled, so without this the latch can never
        fire on a stuck device with no state events.
        """
        presence_st = self.hass.states.get(self._presence_eid)
        presence = presence_st.state if presence_st is not None else None
        self._presence_state = presence
        presence_on = presence == "on"
        is_resetting = self._controller.is_resetting

        # Latch tracking — frozen during reset to prevent phantom echo accumulation.
        if not is_resetting:
            if presence_on:
                if self._presence_on_since is None:
                    self._presence_on_since = now
            else:
                self._presence_on_since = None

        # Mode transitions — latch check runs even during a reset.
        if self._mode == MODE_NORMAL:
            held = (
                self._presence_on_since is not None
                and (now - self._presence_on_since) >= RECOVERY_PRESENCE_ON_SEC
            )
            if held:
                self._enter_recovery("latch", now)
        else:  # MODE_RECOVERY
            # Exit only on a real 'off' that is not an echo of our own cycle.
            if (not is_resetting) and presence == "off":
                self._exit_recovery(now)

        # Output — frozen in NORMAL during reset (phantom echo suppression).
        if self._mode == MODE_NORMAL:
            if not is_resetting:
                self._attr_is_on = presence_on
            # else: keep previous _attr_is_on (freeze)
        else:  # MODE_RECOVERY
            self._attr_is_on = self._compute_output(presence_on)

        self._notify_status()
        if getattr(self, "entity_id", None) is not None:
            self.async_write_ha_state()

    def _compute_output(self, presence_on: bool) -> bool:
        if self._mode == MODE_NORMAL:
            return presence_on
        target = self._read(self._target_distance_eid)
        detect = self._read(self._detection_range_eid)
        if target is None or detect is None:
            return False
        shield = self._read(self._shield_range_eid) or 0.0
        return in_range(target, shield, detect, SHIELD_FLOOR_M)

    def _enter_recovery(self, reason: str, now: float) -> None:
        if self._mode == MODE_RECOVERY:
            return
        self._mode = MODE_RECOVERY
        self._last_reset_anchor = now
        _LOGGER.info(
            "sanitized_presence: %s entering RECOVERY (reason=%s)", self._device_id, reason
        )
        if self._reset_task is None or self._reset_task.done():
            self._reset_task = self.hass.async_create_task(self._controller.request_reset(reason))

    def _exit_recovery(self, now: float) -> None:
        self._mode = MODE_NORMAL
        self._last_reset_anchor = now
        _LOGGER.info("sanitized_presence: %s leaving RECOVERY (real presence=off)", self._device_id)

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers=self._device_identifiers)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "device_id": self._device_id,
            "mode": self._mode,
            "presence_eid": self._presence_eid,
            "presence_state": self._presence_state,
            "target_distance_eid": self._target_distance_eid,
            "detection_range_eid": self._detection_range_eid,
            "shield_range_eid": self._shield_range_eid,
        }
