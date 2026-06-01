"""Heal orchestration for stuck MTG075/MTG275 radars.

Owns the confirmed off->on toggle and the post-toggle observe loop that
judges whether the radar recovered (occupied / empty) or needs another
iteration, bounded by HEAL_MAX_ITERATIONS. The binary sensor decides
*when* to heal (the discrepancy trigger) and delegates the *how* here.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .const import (
    HEAL_CMD_TIMEOUT_SEC,
    HEAL_MAX_ITERATIONS,
    HEAL_OBSERVE_SEC,
    HEAL_POLL_SEC,
    SENSOR_RESET_SEQUENCE,
    STOP_A_CONFIRM_SEC,
)

_LOGGER = logging.getLogger(__name__)

_IGNORED_SELECT = {"unknown", "unavailable"}


class HealController:
    """Drives the confirmed toggle + observe loop for one radar."""

    def __init__(
        self,
        hass: HomeAssistant,
        device_id: str,
        device_name: str,
        sensor_eid: str,
    ) -> None:
        self.hass = hass
        self._device_id = device_id
        self._device_name = device_name
        self._sensor_eid = sensor_eid
        self._healing = False
        self._iteration = 0
        self._presence_on: Callable[[], bool] = lambda: False
        self._in_range_now: Callable[[], bool] = lambda: False

    def bind_observers(
        self,
        presence_on: Callable[[], bool],
        in_range_now: Callable[[], bool],
    ) -> None:
        """Inject readers for native presence and in-range distance."""
        self._presence_on = presence_on
        self._in_range_now = in_range_now

    @property
    def is_healing(self) -> bool:
        """True while a heal run is in flight (echo/guard gate)."""
        return self._healing

    def diagnostics(self) -> dict[str, Any]:
        """Snapshot for the status sensor."""
        return {"healing": self._healing, "iteration": self._iteration}
