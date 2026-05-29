"""Recovery orchestration for stuck MTG075/MTG275 radars.

Owns the firmware reset cycle (walking the device's select entity through
SENSOR_RESET_SEQUENCE) and all safety rails: a per-device re-entrancy
guard, a post-reset cooldown, and a sliding-window rate limit backed by a
circuit breaker. The binary sensor decides *when* recovery is needed and
delegates the *how* to this controller, keeping the entity thin.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .const import (
    RADAR_RESTART_DELAY,
    RESET_COOLDOWN_SEC,
    RESET_RATE_BLOCK_SEC,
    RESET_RATE_LIMIT,
    RESET_RATE_WINDOW_SEC,
    SENSOR_PHASE_DELAY_SEC,
    SENSOR_RESET_SEQUENCE,
)

_LOGGER = logging.getLogger(__name__)


class RecoveryController:
    """Drives the radar reset cycle with cooldown and rate-limit rails."""

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
        self._resetting = False
        self._last_reset_ts: float | None = None
        self._reset_history: list[float] = []
        self._block_until: float = 0.0
        self._last_reason: str | None = None

    @property
    def is_resetting(self) -> bool:
        """True while a reset cycle is in flight (echo-suppression gate)."""
        return self._resetting

    def _prune_history(self, now: float) -> list[float]:
        threshold = now - RESET_RATE_WINDOW_SEC
        self._reset_history = [t for t in self._reset_history if t >= threshold]
        return self._reset_history

    def _record_reset(self, now: float) -> None:
        self._last_reset_ts = now
        self._prune_history(now)
        self._reset_history.append(now)

    def _allow_reset(self, now: float) -> bool:
        if self._block_until and now < self._block_until:
            return False
        if self._last_reset_ts is not None and (now - self._last_reset_ts) < RESET_COOLDOWN_SEC:
            return False
        history = self._prune_history(now)
        if len(history) >= RESET_RATE_LIMIT:
            self._block_until = now + RESET_RATE_BLOCK_SEC
            _LOGGER.warning(
                "sanitized_presence: %s circuit-breaker tripped (%d resets/%ds), "
                "blocking for %ds",
                self._device_name,
                len(history),
                RESET_RATE_WINDOW_SEC,
                RESET_RATE_BLOCK_SEC,
            )
            return False
        return True

    async def request_reset(self, reason: str) -> bool:
        """Start a reset cycle if the safety rails allow it.

        Returns True if a cycle started, False if blocked (re-entrant,
        cooldown, or circuit breaker).
        """
        if self._resetting:
            return False
        if not self._allow_reset(now=time.time()):
            _LOGGER.debug(
                "sanitized_presence: %s reset blocked by rails (reason=%s)",
                self._device_name,
                reason,
            )
            return False
        # Close the echo-suppression gate synchronously, before any await,
        # so a real presence=off arriving between scheduling and the cycle
        # starting cannot be mistaken for recovery (it would otherwise exit
        # RECOVERY prematurely while is_resetting was still False).
        self._resetting = True
        await self.async_reset(reason)
        return True

    async def async_reset(self, reason: str) -> None:
        """Walk the select through SENSOR_RESET_SEQUENCE with phase delays.

        The first "off" is held for RADAR_RESTART_DELAY so the firmware
        de-energizes; remaining phases wait SENSOR_PHASE_DELAY_SEC. Specific
        service errors abort the cycle (the off-fallback is the net for a
        select left parked in "off"); CancelledError propagates so removal
        cancels cleanly.

        Sets the re-entrancy / echo-suppression gate so it is safe to call
        directly; request_reset may have already set it.
        """
        self._resetting = True
        self._last_reason = reason
        try:
            _LOGGER.info(
                "sanitized_presence: %s reset cycle start (reason=%s) -> %s",
                self._device_name,
                reason,
                list(SENSOR_RESET_SEQUENCE),
            )
            await self._select_option(SENSOR_RESET_SEQUENCE[0])
            await asyncio.sleep(RADAR_RESTART_DELAY)
            for option in SENSOR_RESET_SEQUENCE[1:]:
                await asyncio.sleep(SENSOR_PHASE_DELAY_SEC)
                await self._select_option(option)
            self._record_reset(time.time())
            _LOGGER.info(
                "sanitized_presence: %s reset cycle done at option=%s",
                self._device_name,
                SENSOR_RESET_SEQUENCE[-1],
            )
        except asyncio.CancelledError:  # pylint: disable=try-except-raise
            raise
        except (HomeAssistantError, vol.Invalid) as err:
            _LOGGER.error(
                "sanitized_presence: %s reset cycle aborted (eid=%s): %s",
                self._device_name,
                self._sensor_eid,
                err,
            )
        finally:
            self._resetting = False

    async def _select_option(self, option: str) -> None:
        await self.hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": self._sensor_eid, "option": option},
            blocking=True,
        )

    async def maybe_recover_parked(self) -> None:
        """Restore a select parked in any non-'on' state to 'on'.

        A completed cycle ends in 'on', so a select stuck in 'off' or
        'unoccupied' (with no cycle running) means a cycle was interrupted
        (e.g. integration restart, dropped phase command on edge-of-mesh
        radio). This bypasses cooldown/rate limits on purpose — it is a
        recovery tool, not a reset.

        Ignores 'unknown' / 'unavailable' so a temporarily offline device
        is not whipsawed by spurious 'on' commands.
        """
        if self._resetting:
            return
        state = self.hass.states.get(self._sensor_eid)
        if state is None:
            return
        if state.state in ("on", "unknown", "unavailable"):
            return
        _LOGGER.info(
            "sanitized_presence: %s park-fallback — select parked in %r, restoring to 'on'",
            self._device_name,
            state.state,
        )
        await self._select_option("on")

    def diagnostics(self, now: float | None = None) -> dict[str, Any]:
        """Snapshot of safety-rail state for the status sensor."""
        now = now if now is not None else time.time()
        history = self._prune_history(now)
        cooldown_left = 0
        if self._last_reset_ts is not None:
            cooldown_left = max(0, int(RESET_COOLDOWN_SEC - (now - self._last_reset_ts)))
        block_left = max(0, int(self._block_until - now))
        return {
            "resetting": self._resetting,
            "last_reset_ts": self._last_reset_ts,
            "last_reason": self._last_reason,
            "cooldown_left": cooldown_left,
            "rate_window_count": len(history),
            "circuit_breaker_left": block_left,
        }
