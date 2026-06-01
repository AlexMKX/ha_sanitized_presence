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

    async def _select_option(self, option: str) -> None:
        await self.hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": self._sensor_eid, "option": option},
            blocking=True,
        )

    async def _wait_select(self, target: str) -> bool:
        """Poll the select entity until it reports target, up to the timeout.

        Returns True on confirm, False on timeout. Relies on Z2M
        optimistic=off so the select state reflects the real device echo;
        with optimistic on it simply confirms instantly (graceful degrade).
        """
        deadline = time.monotonic() + HEAL_CMD_TIMEOUT_SEC
        while time.monotonic() < deadline:
            st = self.hass.states.get(self._sensor_eid)
            if st is not None and st.state == target:
                return True
            await asyncio.sleep(HEAL_POLL_SEC)
        return False

    async def _confirmed_toggle(self) -> bool:
        """Drive SENSOR_RESET_SEQUENCE, waiting for each step to confirm.

        Returns True if every step confirmed; False if any timed out (the
        cluster write likely did not reach the radar). Even on timeout the
        remaining steps are still attempted so the radar is left at 'on'.
        """
        all_ok = True
        for option in SENSOR_RESET_SEQUENCE:
            await self._select_option(option)
            if not await self._wait_select(option):
                all_ok = False
                _LOGGER.warning(
                    "sanitized_presence: %s select '%s' not confirmed in %ss "
                    "(cluster write may not have reached the radar)",
                    self._device_name,
                    option,
                    HEAL_CMD_TIMEOUT_SEC,
                )
        return all_ok

    async def _observe(self) -> str | None:
        """Watch presence/distance for one HEAL_OBSERVE_SEC window.

        Returns:
            "occupied" — in_range AND presence=on sustained STOP_A_CONFIRM_SEC.
            "empty"    — presence=off for the entire window.
            None       — neither resolved; caller should iterate.
        """
        start = time.monotonic()
        a_since: float | None = None
        ever_present = False
        while time.monotonic() - start < HEAL_OBSERVE_SEC:
            present = bool(self._presence_on())
            in_range = bool(self._in_range_now())
            if present:
                ever_present = True
            if present and in_range:
                if a_since is None:
                    a_since = time.monotonic()
                elif time.monotonic() - a_since >= STOP_A_CONFIRM_SEC:
                    return "occupied"
            else:
                a_since = None
            await asyncio.sleep(HEAL_POLL_SEC)
        if not ever_present:
            return "empty"
        return None

    async def async_heal(self) -> str:
        """Run confirmed toggle + observe, iterating until resolved or capped.

        Returns "occupied", "empty", or "gave_up". Sets the is_healing gate
        for the whole run. CancelledError propagates so entity removal
        cancels cleanly; specific service errors end the run as "gave_up".
        """
        self._healing = True
        self._iteration = 0
        try:
            for i in range(1, HEAL_MAX_ITERATIONS + 1):
                self._iteration = i
                _LOGGER.info(
                    "sanitized_presence: %s heal iteration %d/%d",
                    self._device_name,
                    i,
                    HEAL_MAX_ITERATIONS,
                )
                await self._confirmed_toggle()
                result = await self._observe()
                if result is not None:
                    _LOGGER.info(
                        "sanitized_presence: %s heal resolved: %s (iteration %d)",
                        self._device_name,
                        result,
                        i,
                    )
                    return result
            _LOGGER.warning(
                "sanitized_presence: %s heal gave up after %d iterations",
                self._device_name,
                HEAL_MAX_ITERATIONS,
            )
            return "gave_up"
        except asyncio.CancelledError:  # pylint: disable=try-except-raise
            raise
        except (HomeAssistantError, vol.Invalid) as err:
            _LOGGER.error(
                "sanitized_presence: %s heal aborted (eid=%s): %s",
                self._device_name,
                self._sensor_eid,
                err,
            )
            return "gave_up"
        finally:
            self._healing = False
            self._iteration = 0
