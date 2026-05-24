"""Auto-resetting binary sensor helper.

Copied from ha_door_occupancy and extended with:
- pulse(timeout=None): if timeout is given, overrides ctor reset_timeout
  for this pulse only.
- _notify_deadline(expiry_dt): hook called on pulse (with expiry datetime)
  and on reset (with None). Base implementation is a no-op; subclasses
  override to update a companion DeadlineSensorEntity.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util


class AutoResetBinarySensor(BinarySensorEntity):
    """Binary sensor that pulses on and auto-resets off.

    Subclasses call pulse() to turn the sensor on; it automatically resets
    to off after a configured timeout. A new pulse() call cancels and
    reschedules the reset (sliding window).
    """

    def __init__(self, hass: HomeAssistant, reset_timeout: float) -> None:
        self.hass = hass
        self._reset_timeout = float(reset_timeout)
        self._attr_is_on = False
        self._cancel_reset: Callable[[], None] | None = None

    @callback
    def pulse(self, timeout: float | None = None) -> None:
        """Turn on and (re)schedule the reset-to-off callback.

        Args:
            timeout: override the ctor reset_timeout for this pulse only.
                     If None, the ctor value is used.
        """
        self._attr_is_on = True
        if self._cancel_reset is not None:
            self._cancel_reset()
        effective = float(timeout) if timeout is not None else self._reset_timeout
        self._cancel_reset = async_call_later(self.hass, effective, self._on_reset)
        self._notify_deadline(dt_util.utcnow() + timedelta(seconds=effective))
        self.async_write_ha_state()

    @callback
    def _on_reset(self, _now: datetime | None) -> None:
        """Callback fired when the deadline expires."""
        self._cancel_reset = None
        self._attr_is_on = False
        self._notify_deadline(None)
        self.async_write_ha_state()

    def _notify_deadline(self, expiry_dt: datetime | None) -> None:
        """Called on every deadline change. Override in subclasses."""

    async def async_will_remove_from_hass(self) -> None:
        """Cancel the pending reset callback on entity removal."""
        if self._cancel_reset is not None:
            self._cancel_reset()
            self._cancel_reset = None
