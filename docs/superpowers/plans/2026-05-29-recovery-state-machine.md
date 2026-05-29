# Recovery State Machine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the always-on distance gate with a two-state machine (NORMAL = mirror native presence; RECOVERY = distance-mode output + firmware reset cycle), porting the recovery logic from the `occupancy_reset` pyscript into the integration.

**Architecture:** A thin HA entity (`SanitizedPresenceBinarySensor`) owns output and the latch/health timers; a dedicated `RecoveryController` owns the reset cycle and all safety rails (re-entrancy, cooldown, rate-limit/circuit-breaker, off-fallback). A repurposed `StatusSensorEntity` exposes the current mode and controller diagnostics. Discovery gains a required `sensor` (select) entity.

**Tech Stack:** Python 3.x, Home Assistant custom component, `asyncio`, pytest (+ `freezegun` / `async_fire_time_changed` for virtual time). Tests run with `PYTHONPATH=. pytest -q -m "not docker_e2e"` from repo root.

**Spec:** `docs/superpowers/specs/2026-05-29-recovery-state-machine-design.md`

---

## File Structure

| File | Responsibility | Action |
|------|----------------|--------|
| `custom_components/sanitized_presence/const.py` | Constants | Modify: add recovery constants, remove pulse constants |
| `custom_components/sanitized_presence/recovery.py` | Reset cycle + safety rails (NEW) | Create |
| `custom_components/sanitized_presence/binary_sensor.py` | Thin entity: output + latch/health timers | Rewrite |
| `custom_components/sanitized_presence/sensor.py` | `StatusSensorEntity` (mode + diagnostics) | Rewrite |
| `custom_components/sanitized_presence/discovery.py` | Add `sensor` suffix; wire controller | Modify |
| `custom_components/sanitized_presence/auto_reset.py` | Pulse/deadline base (vestigial) | Remove (Task 11, approval gate) |
| `custom_components/sanitized_presence/manifest.json` | Version | Bump (Task 12) |
| `tests/test_recovery.py` | RecoveryController behavior (NEW) | Create |
| `tests/test_binary_sensor.py` | State machine behavior | Rewrite |
| `tests/test_status_sensor.py` | Status sensor (NEW, replaces test_deadline_sensor.py) | Create |
| `tests/test_discovery.py` | Discovery incl. `sensor` suffix | Modify |
| `tests/test_auto_reset.py` | Pulse base tests | Remove (Task 11) |
| `tests/test_deadline_sensor.py` | Deadline sensor tests | Remove (Task 6) |

---

## Task 1: Add recovery constants to const.py

**Files:**
- Modify: `custom_components/sanitized_presence/const.py`

- [ ] **Step 1: Add the new constants and `__all__` entries**

Edit `const.py`. Replace the `# Range evaluation` block onward (the pulse-model constants `DELAY_*`, `DEFAULT_DELAY_S`, `TICK_*` will be removed in Task 7 once unreferenced — leave them for now so the current code keeps importing). Add the following block before `__all__`:

```python
# --- Recovery state machine ---

# Enter recovery after presence has been continuously "on" this long.
RECOVERY_PRESENCE_ON_SEC = 900  # 15 min

# Proactive periodic reset cadence (since last reset / since startup).
HEALTH_RESET_INTERVAL_SEC = 1800

# Select-walk order driven on every reset cycle. Casing matches the option
# labels exposed by Z2M exactly and must not be normalized.
SENSOR_RESET_SEQUENCE = ("off", "unoccupied", "on")

# Hold the first "off" this long so the radar firmware fully de-energizes.
RADAR_RESTART_DELAY = 30

# Debounce between the remaining cycle phases so the Tuya MCU / Z2M can
# acknowledge each transition.
SENSOR_PHASE_DELAY_SEC = 0.5

# Safety rails.
RESET_COOLDOWN_SEC = 120
RESET_RATE_WINDOW_SEC = 1800
RESET_RATE_LIMIT = 3
RESET_RATE_BLOCK_SEC = 1800

# Off-fallback poll cadence: a completed cycle ends in "on", so a select
# parked in "off" indicates an interrupted cycle to recover.
OFF_FALLBACK_INTERVAL_SEC = 60

# Z2M select (operating mode) unique_id suffix.
SUFFIX_SENSOR = "sensor"
```

Add these names to `__all__`:

```python
    "RECOVERY_PRESENCE_ON_SEC",
    "HEALTH_RESET_INTERVAL_SEC",
    "SENSOR_RESET_SEQUENCE",
    "RADAR_RESTART_DELAY",
    "SENSOR_PHASE_DELAY_SEC",
    "RESET_COOLDOWN_SEC",
    "RESET_RATE_WINDOW_SEC",
    "RESET_RATE_LIMIT",
    "RESET_RATE_BLOCK_SEC",
    "OFF_FALLBACK_INTERVAL_SEC",
    "SUFFIX_SENSOR",
```

- [ ] **Step 2: Verify import works**

Run: `PYTHONPATH=. python -c "from custom_components.sanitized_presence import const; print(const.RECOVERY_PRESENCE_ON_SEC, const.SUFFIX_SENSOR)"`
Expected: `900 sensor`

- [ ] **Step 3: Commit**

```bash
git add custom_components/sanitized_presence/const.py
git commit -m "feat(const): add recovery state-machine constants"
```

---

## Task 2: RecoveryController — rate limiter & cooldown (pure logic)

The controller's safety-rail decisions are pure functions of timestamps, so they are tested without HA. Build this core first.

**Files:**
- Create: `custom_components/sanitized_presence/recovery.py`
- Create: `custom_components/sanitized_presence/tests/test_recovery.py`

- [ ] **Step 1: Write failing tests for the rate limiter / cooldown**

Create `tests/test_recovery.py`:

```python
"""Behavior tests for RecoveryController.

Groups:
- TestRateLimit: cooldown + rate-limit/circuit-breaker gating of resets.
- TestResetCycle: the async select-walk and its error handling.
- TestOffFallback: parking-in-off recovery.
- TestDiagnostics: snapshot fields the status sensor reads.

How to run:
    PYTHONPATH=. pytest custom_components/sanitized_presence/tests/test_recovery.py
"""

from __future__ import annotations

# pylint: disable=protected-access

from unittest.mock import MagicMock

import pytest

from custom_components.sanitized_presence.const import (
    RESET_COOLDOWN_SEC,
    RESET_RATE_BLOCK_SEC,
    RESET_RATE_LIMIT,
)
from custom_components.sanitized_presence.recovery import RecoveryController


def _make_controller(hass=None):
    return RecoveryController(
        hass=hass or MagicMock(),
        device_id="dev1",
        device_name="Radar 1",
        sensor_eid="select.radar_sensor",
    )


class TestRateLimit:
    """Cooldown and rate-limit/circuit-breaker decisions are time-pure."""

    def test_allows_first_reset(self):
        """A fresh controller permits an immediate reset.

        Validates: no spurious cooldown/circuit-breaker blocks a device
        that has never been reset.
        Code: custom_components/sanitized_presence/recovery.py::RecoveryController._allow_reset
        Assertion: _allow_reset(now=1000.0) is True.
        Method:
        1. Arrange: fresh controller.
        2. Act: call _allow_reset(1000.0).
        3. Assert: True.
        """
        ctrl = _make_controller()
        assert ctrl._allow_reset(now=1000.0) is True

    def test_cooldown_blocks_back_to_back(self):
        """A reset within RESET_COOLDOWN_SEC of the last one is blocked.

        Validates: the integration never hammers the select entity twice
        in quick succession.
        Code: custom_components/sanitized_presence/recovery.py::RecoveryController._allow_reset
        Assertion: a reset RESET_COOLDOWN_SEC-1 after the last is blocked;
            one exactly at the boundary is allowed.
        Method:
        1. Arrange: record a reset at t=1000.
        2. Act: query _allow_reset just inside and just past cooldown.
        3. Assert: inside -> False, past -> True.
        """
        ctrl = _make_controller()
        ctrl._record_reset(1000.0)
        assert ctrl._allow_reset(now=1000.0 + RESET_COOLDOWN_SEC - 1) is False
        assert ctrl._allow_reset(now=1000.0 + RESET_COOLDOWN_SEC) is True

    def test_circuit_breaker_trips_after_rate_limit(self):
        """Exceeding RESET_RATE_LIMIT resets in the window trips the breaker.

        Validates: a runaway reset loop is capped to protect the Zigbee
        mesh; once tripped, further resets are blocked for the block window.
        Code: custom_components/sanitized_presence/recovery.py::RecoveryController._allow_reset
        Assertion: after RESET_RATE_LIMIT recorded resets, the next
            _allow_reset is False, and remains False until
            RESET_RATE_BLOCK_SEC elapses.
        Method:
        1. Arrange: record RESET_RATE_LIMIT resets spaced past cooldown.
        2. Act: query _allow_reset right after, and after the block window.
        3. Assert: blocked, then allowed.
        """
        ctrl = _make_controller()
        t = 1000.0
        for _ in range(RESET_RATE_LIMIT):
            ctrl._record_reset(t)
            t += RESET_COOLDOWN_SEC  # spaced so cooldown alone wouldn't block
        blocked_at = t
        assert ctrl._allow_reset(now=blocked_at) is False
        assert ctrl._allow_reset(now=blocked_at + RESET_RATE_BLOCK_SEC) is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=. pytest custom_components/sanitized_presence/tests/test_recovery.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named '...recovery'`

- [ ] **Step 3: Implement RecoveryController core**

Create `recovery.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=. pytest custom_components/sanitized_presence/tests/test_recovery.py::TestRateLimit -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add custom_components/sanitized_presence/recovery.py custom_components/sanitized_presence/tests/test_recovery.py
git commit -m "feat(recovery): RecoveryController cooldown and rate-limit core"
```

---

## Task 3: RecoveryController — async reset cycle

**Files:**
- Modify: `custom_components/sanitized_presence/recovery.py`
- Modify: `custom_components/sanitized_presence/tests/test_recovery.py`

- [ ] **Step 1: Write failing tests for the reset cycle**

Append to `tests/test_recovery.py`:

```python
class TestResetCycle:
    """request_reset drives the select through SENSOR_RESET_SEQUENCE."""

    @pytest.mark.asyncio
    async def test_reset_walks_sequence_in_order(self, monkeypatch):
        """A reset calls select.select_option for each phase in order.

        Validates: the firmware-recovery contract — the select is walked
        off -> unoccupied -> on. The order IS the contract, so exact call
        order is asserted intentionally.
        Code: custom_components/sanitized_presence/recovery.py::RecoveryController.async_reset
        Assertion: hass.services.async_call invoked once per sequence
            option, in SENSOR_RESET_SEQUENCE order, on the sensor eid.
        Method:
        1. Arrange: controller with a mock hass; patch asyncio.sleep.
        2. Act: await async_reset("test").
        3. Assert: call options equal list(SENSOR_RESET_SEQUENCE).
        """
        import custom_components.sanitized_presence.recovery as rec
        from custom_components.sanitized_presence.const import SENSOR_RESET_SEQUENCE

        async def _no_sleep(_):
            return None

        monkeypatch.setattr(rec.asyncio, "sleep", _no_sleep)

        hass = MagicMock()
        calls = []

        async def _async_call(domain, service, data, blocking=False):
            calls.append((domain, service, data["option"], data["entity_id"]))

        hass.services.async_call = _async_call
        ctrl = _make_controller(hass)

        await ctrl.async_reset("test")

        assert [c[2] for c in calls] == list(SENSOR_RESET_SEQUENCE)
        assert all(c[3] == "select.radar_sensor" for c in calls)
        assert all(c[0] == "select" and c[1] == "select_option" for c in calls)

    @pytest.mark.asyncio
    async def test_reset_blocked_by_cooldown_does_not_call_service(self, monkeypatch):
        """request_reset within cooldown performs no select_option calls.

        Validates: the cooldown rail actually prevents side effects, not
        just returns False.
        Code: custom_components/sanitized_presence/recovery.py::RecoveryController.request_reset
        Assertion: with a recent reset recorded, request_reset returns
            False and async_call is never invoked.
        Method:
        1. Arrange: record a reset at "now"; freeze time just after.
        2. Act: await request_reset("test").
        3. Assert: returns False; no service calls.
        """
        import custom_components.sanitized_presence.recovery as rec

        monkeypatch.setattr(rec.time, "time", lambda: 1000.0)
        hass = MagicMock()
        calls = []

        async def _async_call(*args, **kwargs):
            calls.append(args)

        hass.services.async_call = _async_call
        ctrl = _make_controller(hass)
        ctrl._record_reset(1000.0)

        started = await ctrl.request_reset("test")

        assert started is False
        assert calls == []

    @pytest.mark.asyncio
    async def test_reset_failure_aborts_and_clears_resetting(self, monkeypatch):
        """A failing select_option aborts the cycle and clears is_resetting.

        Validates: fail-fast error handling — a service error does not
        leave the echo-suppression gate stuck closed, and remaining phases
        are not attempted after the failure.
        Code: custom_components/sanitized_presence/recovery.py::RecoveryController.async_reset
        Assertion: async_reset raises/handles, is_resetting is False after,
            and no phases run past the failing one.
        Method:
        1. Arrange: async_call raises HomeAssistantError on first call.
        2. Act: await async_reset("test").
        3. Assert: is_resetting False; exactly one call attempted.
        """
        import custom_components.sanitized_presence.recovery as rec
        from homeassistant.exceptions import HomeAssistantError

        async def _no_sleep(_):
            return None

        monkeypatch.setattr(rec.asyncio, "sleep", _no_sleep)
        hass = MagicMock()
        calls = []

        async def _async_call(*args, **kwargs):
            calls.append(args)
            raise HomeAssistantError("boom")

        hass.services.async_call = _async_call
        ctrl = _make_controller(hass)

        await ctrl.async_reset("test")

        assert ctrl.is_resetting is False
        assert len(calls) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=. pytest custom_components/sanitized_presence/tests/test_recovery.py::TestResetCycle -v`
Expected: FAIL with `AttributeError: ... has no attribute 'async_reset'`

- [ ] **Step 3: Implement async_reset and request_reset**

Append these methods to `RecoveryController` in `recovery.py`:

```python
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
        await self.async_reset(reason)
        return True

    async def async_reset(self, reason: str) -> None:
        """Walk the select through SENSOR_RESET_SEQUENCE with phase delays.

        The first "off" is held for RADAR_RESTART_DELAY so the firmware
        de-energizes; remaining phases wait SENSOR_PHASE_DELAY_SEC. Specific
        service errors abort the cycle (the off-fallback is the net for a
        select left parked in "off"); CancelledError propagates so removal
        cancels cleanly.
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
        except asyncio.CancelledError:
            raise
        except HomeAssistantError as err:
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=. pytest custom_components/sanitized_presence/tests/test_recovery.py::TestResetCycle -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add custom_components/sanitized_presence/recovery.py custom_components/sanitized_presence/tests/test_recovery.py
git commit -m "feat(recovery): async reset cycle with fail-fast error handling"
```

---

## Task 4: RecoveryController — off-fallback

**Files:**
- Modify: `custom_components/sanitized_presence/recovery.py`
- Modify: `custom_components/sanitized_presence/tests/test_recovery.py`

- [ ] **Step 1: Write failing tests for off-fallback**

Append to `tests/test_recovery.py`:

```python
class TestOffFallback:
    """maybe_recover_off flips a select parked in 'off' back to 'on'."""

    @pytest.mark.asyncio
    async def test_off_select_is_restored_to_on(self):
        """A select reading 'off' (no cycle running) is restored to 'on'.

        Validates: the last-resort guard recovers a cycle interrupted by an
        integration restart, where the select would otherwise stay 'off'.
        Code: custom_components/sanitized_presence/recovery.py::RecoveryController.maybe_recover_off
        Assertion: select_option('on') is called once.
        Method:
        1. Arrange: hass.states.get(sensor_eid).state == 'off'; not resetting.
        2. Act: await maybe_recover_off().
        3. Assert: one select_option call with option 'on'.
        """
        hass = MagicMock()
        state = MagicMock()
        state.state = "off"
        hass.states.get.return_value = state
        calls = []

        async def _async_call(domain, service, data, blocking=False):
            calls.append(data["option"])

        hass.services.async_call = _async_call
        ctrl = _make_controller(hass)

        await ctrl.maybe_recover_off()

        assert calls == ["on"]

    @pytest.mark.asyncio
    async def test_off_fallback_skipped_while_resetting(self):
        """The fallback does not interfere with an in-flight reset cycle.

        Validates: maybe_recover_off respects the re-entrancy guard so it
        never collides with the legitimate 'off' phase of a running cycle.
        Code: custom_components/sanitized_presence/recovery.py::RecoveryController.maybe_recover_off
        Assertion: with is_resetting True, no service call is made even if
            the select reads 'off'.
        Method:
        1. Arrange: select state 'off'; set _resetting True.
        2. Act: await maybe_recover_off().
        3. Assert: no service calls.
        """
        hass = MagicMock()
        state = MagicMock()
        state.state = "off"
        hass.states.get.return_value = state
        calls = []

        async def _async_call(*args, **kwargs):
            calls.append(args)

        hass.services.async_call = _async_call
        ctrl = _make_controller(hass)
        ctrl._resetting = True

        await ctrl.maybe_recover_off()

        assert calls == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=. pytest custom_components/sanitized_presence/tests/test_recovery.py::TestOffFallback -v`
Expected: FAIL with `AttributeError: ... 'maybe_recover_off'`

- [ ] **Step 3: Implement maybe_recover_off**

Append to `RecoveryController` in `recovery.py`:

```python
    async def maybe_recover_off(self) -> None:
        """Restore a select parked in 'off' to 'on'.

        A completed cycle ends in 'on', so a select stuck in 'off' (with no
        cycle running) means a cycle was interrupted (e.g. integration
        restart). This bypasses cooldown/rate limits on purpose — it is a
        recovery tool, not a reset.
        """
        if self._resetting:
            return
        state = self.hass.states.get(self._sensor_eid)
        if state is not None and state.state == "off":
            _LOGGER.info(
                "sanitized_presence: %s off-fallback — select parked in 'off', "
                "restoring to 'on'",
                self._device_name,
            )
            await self._select_option("on")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=. pytest custom_components/sanitized_presence/tests/test_recovery.py::TestOffFallback -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add custom_components/sanitized_presence/recovery.py custom_components/sanitized_presence/tests/test_recovery.py
git commit -m "feat(recovery): off-fallback for interrupted cycles"
```

---

## Task 5: Rewrite binary_sensor.py — state machine

This is the core rewrite. The entity has two states; output is a property, not a pulse. It tracks the presence-on start time and a last-reset anchor to drive the two recovery triggers, and reads `RecoveryController.is_resetting` to suppress presence echoes.

**Files:**
- Rewrite: `custom_components/sanitized_presence/binary_sensor.py`
- Rewrite: `custom_components/sanitized_presence/tests/test_binary_sensor.py`

- [ ] **Step 1: Write the failing tests (state machine behavior)**

Replace the entire contents of `tests/test_binary_sensor.py` with:

```python
"""Behavior tests for SanitizedPresenceBinarySensor (state machine).

Groups:
- TestNormalMirror: NORMAL output mirrors the native presence DP.
- TestLatchTrigger: continuous presence=on >= RECOVERY_PRESENCE_ON_SEC enters RECOVERY.
- TestRecoveryOutput: RECOVERY output uses in_range only (presence ignored).
- TestRecoveryExit: a real presence=off after the cycle returns to NORMAL.
- TestEchoSuppression: presence transitions during an active cycle are ignored.

How to run:
    PYTHONPATH=. pytest custom_components/sanitized_presence/tests/test_binary_sensor.py
"""

from __future__ import annotations

# pylint: disable=protected-access

from unittest.mock import MagicMock

import pytest

from custom_components.sanitized_presence.binary_sensor import (
    MODE_NORMAL,
    MODE_RECOVERY,
    SanitizedPresenceBinarySensor,
)


def _make_state(value: str):
    s = MagicMock()
    s.state = value
    return s


def _make_sensor(hass, controller=None):
    entry = MagicMock()
    entry.entry_id = "e1"
    sensor = SanitizedPresenceBinarySensor(
        hass=hass,
        entry=entry,
        device_id="dev1",
        device_name="Radar 1",
        device_identifiers={("zigbee2mqtt", "0xABCD")},
        target_distance_eid="sensor.radar_target_distance",
        detection_range_eid="number.radar_detection_range",
        shield_range_eid="number.radar_shield_range",
        presence_eid="binary_sensor.radar_presence",
        controller=controller or MagicMock(is_resetting=False),
    )
    # Bypass HA entity write during unit tests.
    sensor.async_write_ha_state = MagicMock()
    return sensor


def _states(hass, *, presence="on", target=1.5, detect=4.5, shield=0.0):
    mapping = {
        "binary_sensor.radar_presence": _make_state(presence),
        "sensor.radar_target_distance": _make_state(str(target)),
        "number.radar_detection_range": _make_state(str(detect)),
        "number.radar_shield_range": _make_state(str(shield)),
    }
    hass.states.get.side_effect = mapping.get


class TestNormalMirror:
    """In NORMAL the output equals the native presence DP."""

    @pytest.mark.parametrize(("presence", "expected"), [("on", True), ("off", False)])
    def test_output_mirrors_presence(self, hass, presence, expected):
        """NORMAL output is True iff presence == 'on'.

        Validates: the default behavior — sanitized presence is a faithful
        mirror of the radar's own presence DP until recovery is needed.
        Code: custom_components/sanitized_presence/binary_sensor.py::SanitizedPresenceBinarySensor._compute_output
        Assertion: is_on equals expected for presence on/off.
        Method:
        1. Arrange: NORMAL mode, presence as parametrized.
        2. Act: call _recompute(now=0.0).
        3. Assert: is_on == expected; mode stays NORMAL.
        """
        sensor = _make_sensor(hass)
        _states(hass, presence=presence)
        sensor._recompute(now=0.0)
        assert sensor.is_on is expected
        assert sensor._mode == MODE_NORMAL


class TestLatchTrigger:
    """Continuous presence=on past the threshold enters RECOVERY."""

    @pytest.mark.asyncio
    async def test_presence_on_past_threshold_enters_recovery(self, hass):
        """presence=on held RECOVERY_PRESENCE_ON_SEC enters RECOVERY + resets.

        Validates: the primary latch trigger that detects a radar stuck
        reporting presence and kicks off firmware recovery.
        Code: custom_components/sanitized_presence/binary_sensor.py::SanitizedPresenceBinarySensor._recompute
        Assertion: after the threshold elapses with presence on, mode is
            RECOVERY and controller.request_reset was awaited.
        Method:
        1. Arrange: presence on at t=0 (records on-start); controller mock.
        2. Act: _recompute at t=RECOVERY_PRESENCE_ON_SEC.
        3. Assert: mode RECOVERY; request_reset called.
        """
        from custom_components.sanitized_presence.const import RECOVERY_PRESENCE_ON_SEC

        controller = MagicMock(is_resetting=False)

        async def _req(_reason):
            return True

        controller.request_reset = MagicMock(side_effect=_req)
        sensor = _make_sensor(hass, controller)
        _states(hass, presence="on")

        sensor._recompute(now=0.0)  # arm on-start
        sensor._recompute(now=float(RECOVERY_PRESENCE_ON_SEC))

        assert sensor._mode == MODE_RECOVERY
        controller.request_reset.assert_called()

    def test_intervening_off_resets_the_timer(self, hass):
        """A presence=off resets the on-start timer, preventing entry.

        Validates: only *continuous* presence triggers recovery; normal
        on/off activity must not.
        Code: custom_components/sanitized_presence/binary_sensor.py::SanitizedPresenceBinarySensor._recompute
        Assertion: an off at mid-window resets _presence_on_since so a
            later recompute (still before a fresh full window) stays NORMAL.
        Method:
        1. Arrange: presence on at t=0; off at t=500; on again at t=600.
        2. Act: recompute at t=1000 (only 400s continuous).
        3. Assert: mode NORMAL.
        """
        from custom_components.sanitized_presence.const import RECOVERY_PRESENCE_ON_SEC

        assert RECOVERY_PRESENCE_ON_SEC == 900  # guard: math below assumes this
        sensor = _make_sensor(hass)

        _states(hass, presence="on")
        sensor._recompute(now=0.0)
        _states(hass, presence="off")
        sensor._recompute(now=500.0)
        _states(hass, presence="on")
        sensor._recompute(now=600.0)
        sensor._recompute(now=1000.0)  # only 400s continuous on

        assert sensor._mode == MODE_NORMAL


class TestRecoveryOutput:
    """In RECOVERY the output uses in_range only; presence is ignored."""

    @pytest.mark.parametrize(
        ("target", "expected"),
        [(1.5, True), (5.0, False), (0.0, False)],
    )
    def test_recovery_output_is_in_range(self, hass, target, expected):
        """RECOVERY output reflects in_range(shield<target<detect) only.

        Validates: during recovery the untrusted presence DP is dropped and
        the measured target distance decides the output.
        Code: custom_components/sanitized_presence/binary_sensor.py::SanitizedPresenceBinarySensor._compute_output
        Assertion: with presence forced 'on', output tracks in_range of the
            target, not presence.
        Method:
        1. Arrange: force mode RECOVERY; presence 'on'; vary target.
        2. Act: _recompute.
        3. Assert: is_on == expected.
        """
        controller = MagicMock(is_resetting=True)
        sensor = _make_sensor(hass, controller)
        sensor._mode = MODE_RECOVERY
        _states(hass, presence="on", target=target, detect=4.5, shield=0.0)

        sensor._recompute(now=10.0)

        assert sensor.is_on is expected


class TestRecoveryExit:
    """A real presence=off after the cycle returns to NORMAL."""

    def test_real_off_after_cycle_returns_to_normal(self, hass):
        """presence=off observed when not resetting exits RECOVERY.

        Validates: the recovery exit condition — a genuine clear (not an
        echo of our own cycle) proves the firmware is alive again.
        Code: custom_components/sanitized_presence/binary_sensor.py::SanitizedPresenceBinarySensor._recompute
        Assertion: in RECOVERY, controller not resetting, presence 'off'
            -> mode NORMAL and output False.
        Method:
        1. Arrange: mode RECOVERY; controller.is_resetting False; presence off.
        2. Act: _recompute.
        3. Assert: mode NORMAL; is_on False.
        """
        controller = MagicMock(is_resetting=False)
        sensor = _make_sensor(hass, controller)
        sensor._mode = MODE_RECOVERY
        _states(hass, presence="off")

        sensor._recompute(now=10.0)

        assert sensor._mode == MODE_NORMAL
        assert sensor.is_on is False


class TestEchoSuppression:
    """Presence transitions during an active cycle are ignored."""

    def test_presence_off_during_cycle_does_not_exit(self, hass):
        """An 'off' while controller.is_resetting stays in RECOVERY.

        Validates: the echo-suppression gate — the 'off' phase of our own
        reset cycle must not be mistaken for a real clear.
        Code: custom_components/sanitized_presence/binary_sensor.py::SanitizedPresenceBinarySensor._recompute
        Assertion: in RECOVERY with is_resetting True and presence 'off',
            mode remains RECOVERY.
        Method:
        1. Arrange: mode RECOVERY; controller.is_resetting True; presence off.
        2. Act: _recompute.
        3. Assert: mode stays RECOVERY.
        """
        controller = MagicMock(is_resetting=True)
        sensor = _make_sensor(hass, controller)
        sensor._mode = MODE_RECOVERY
        _states(hass, presence="off")

        sensor._recompute(now=10.0)

        assert sensor._mode == MODE_RECOVERY
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=. pytest custom_components/sanitized_presence/tests/test_binary_sensor.py -v`
Expected: FAIL with `ImportError: cannot import name 'MODE_NORMAL'`

- [ ] **Step 3: Rewrite binary_sensor.py**

Replace the entire contents of `binary_sensor.py` with:

```python
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

import logging
import time
from collections.abc import Callable
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
            self.hass, self._on_health_tick, _interval(HEALTH_RESET_INTERVAL_SEC)
        )
        self._unsub_fallback = async_track_time_interval(
            self.hass, self._on_fallback_tick, _interval(OFF_FALLBACK_INTERVAL_SEC)
        )
        self._recompute(now=time.time())
        _LOGGER.info("SanitizedPresenceBinarySensor created for device_id=%s", self._device_id)

    async def async_will_remove_from_hass(self) -> None:
        for unsub_attr in ("_unsub_state", "_unsub_health", "_unsub_fallback"):
            unsub = getattr(self, unsub_attr)
            if unsub is not None:
                unsub()
                setattr(self, unsub_attr, None)

    @callback
    def _handle_source_event(self, event) -> None:
        new_state = event.data.get("new_state")
        if new_state is None or new_state.state in _IGNORED_STATES:
            return
        self._recompute(now=time.time())

    @callback
    def _on_health_tick(self, _now) -> None:
        now = time.time()
        if self._mode == MODE_NORMAL and (now - self._last_reset_anchor) >= HEALTH_RESET_INTERVAL_SEC:
            self._enter_recovery("health", now)
        self._recompute(now=now)

    @callback
    def _on_fallback_tick(self, _now) -> None:
        self.hass.async_create_task(self._controller.maybe_recover_off())

    def _read(self, eid: str) -> float | None:
        s = self.hass.states.get(eid)
        return _to_float(s.state if s else None)

    @callback
    def _recompute(self, now: float) -> None:
        """Single decision point: update mode, output, and recovery triggers."""
        presence_st = self.hass.states.get(self._presence_eid)
        presence = presence_st.state if presence_st is not None else None
        self._presence_state = presence
        presence_on = presence == "on"

        # Track continuous presence-on duration for the latch trigger.
        if presence_on:
            if self._presence_on_since is None:
                self._presence_on_since = now
        else:
            self._presence_on_since = None

        if self._mode == MODE_NORMAL:
            held = self._presence_on_since is not None and (
                now - self._presence_on_since
            ) >= RECOVERY_PRESENCE_ON_SEC
            if held:
                self._enter_recovery("latch", now)
        else:  # RECOVERY
            # Exit only on a real 'off' that is not an echo of our own cycle.
            if (not self._controller.is_resetting) and presence == "off":
                self._exit_recovery(now)

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
        self.hass.async_create_task(self._controller.request_reset(reason))

    def _exit_recovery(self, now: float) -> None:
        self._mode = MODE_NORMAL
        self._last_reset_anchor = now
        _LOGGER.info(
            "sanitized_presence: %s leaving RECOVERY (real presence=off)", self._device_id
        )

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


def _interval(seconds: int):
    from datetime import timedelta

    return timedelta(seconds=seconds)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=. pytest custom_components/sanitized_presence/tests/test_binary_sensor.py -v`
Expected: PASS (all groups)

- [ ] **Step 5: Commit**

```bash
git add custom_components/sanitized_presence/binary_sensor.py custom_components/sanitized_presence/tests/test_binary_sensor.py
git commit -m "feat(binary_sensor): two-state recovery machine replacing pulse model"
```

---

## Task 6: Repurpose sensor.py — StatusSensorEntity

**Files:**
- Rewrite: `custom_components/sanitized_presence/sensor.py`
- Create: `custom_components/sanitized_presence/tests/test_status_sensor.py`
- Remove: `custom_components/sanitized_presence/tests/test_deadline_sensor.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_status_sensor.py`:

```python
"""Behavior tests for StatusSensorEntity.

Group:
- TestStatus: native_value is the current mode; attributes carry diagnostics.

How to run:
    PYTHONPATH=. pytest custom_components/sanitized_presence/tests/test_status_sensor.py
"""

from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.sanitized_presence.sensor import StatusSensorEntity


def _make_sensor(hass):
    entry = MagicMock()
    entry.entry_id = "e1"
    sensor = StatusSensorEntity(
        hass=hass,
        entry=entry,
        device_id="dev1",
        device_name="Radar 1",
        device_identifiers={("zigbee2mqtt", "0xABCD")},
    )
    sensor.async_write_ha_state = MagicMock()
    return sensor


class TestStatus:
    """native_value reflects mode; extra_state_attributes carry diagnostics."""

    def test_set_status_updates_value_and_attributes(self, hass):
        """set_status records the mode as value and diagnostics as attributes.

        Validates: the diagnostic surface a user inspects to see whether a
        device is recovering and how the safety rails stand.
        Code: custom_components/sanitized_presence/sensor.py::StatusSensorEntity.set_status
        Assertion: native_value == 'recovery'; attributes expose
            cooldown_left and rate_window_count from the snapshot.
        Method:
        1. Arrange: build sensor.
        2. Act: set_status('recovery', {diagnostics...}).
        3. Assert: value and attributes reflect the inputs.
        """
        sensor = _make_sensor(hass)
        sensor.set_status(
            "recovery",
            {"cooldown_left": 42, "rate_window_count": 2, "circuit_breaker_left": 0},
        )
        assert sensor.native_value == "recovery"
        attrs = sensor.extra_state_attributes
        assert attrs["cooldown_left"] == 42
        assert attrs["rate_window_count"] == 2

    def test_unique_id_is_preserved_for_migration(self, hass):
        """unique_id keeps the legacy suffix so the registry entity is reused.

        Validates: repurposing the deadline sensor must not orphan the
        existing HA entity; the unique_id stays stable.
        Code: custom_components/sanitized_presence/sensor.py::StatusSensorEntity.__init__
        Assertion: unique_id ends with the historical
            '_sanitized_presence_deadline' suffix.
        Method:
        1. Arrange/Act: build sensor.
        2. Assert: unique_id == 'dev1_sanitized_presence_deadline'.
        """
        sensor = _make_sensor(hass)
        assert sensor.unique_id == "dev1_sanitized_presence_deadline"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=. pytest custom_components/sanitized_presence/tests/test_status_sensor.py -v`
Expected: FAIL with `ImportError: cannot import name 'StatusSensorEntity'`

- [ ] **Step 3: Rewrite sensor.py**

Replace the entire contents of `sensor.py` with:

```python
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
```

- [ ] **Step 4: Remove the obsolete deadline test**

Run: `git rm custom_components/sanitized_presence/tests/test_deadline_sensor.py`

- [ ] **Step 5: Run tests to verify they pass**

Run: `PYTHONPATH=. pytest custom_components/sanitized_presence/tests/test_status_sensor.py -v`
Expected: PASS (2 tests)

- [ ] **Step 6: Commit**

```bash
git add custom_components/sanitized_presence/sensor.py custom_components/sanitized_presence/tests/test_status_sensor.py
git commit -m "feat(sensor): repurpose deadline sensor into StatusSensorEntity"
```

---

## Task 7: Remove pulse-model constants from const.py

Now that `binary_sensor.py` no longer imports them, drop the dead pulse constants.

**Files:**
- Modify: `custom_components/sanitized_presence/const.py`

- [ ] **Step 1: Confirm nothing references the pulse constants**

Run: `rg -n 'DEFAULT_DELAY_S|DELAY_MIN_S|DELAY_MAX_S|TICK_FLOOR_S|TICK_CEILING_S|SUFFIX_DEPARTURE_DELAY' custom_components/sanitized_presence --glob '!tests/*' --glob '!const.py'`
Expected: no matches (only `const.py` / tests).

If `SUFFIX_DEPARTURE_DELAY` still appears in `discovery.py`, it is removed in Task 8 — leave it here until then and only remove the `DELAY_*`/`TICK_*`/`DEFAULT_DELAY_S` constants in this task.

- [ ] **Step 2: Remove the constants**

In `const.py`, delete these lines and their `__all__` entries:

```python
DELAY_MIN_S = 10
DELAY_MAX_S = 600
DEFAULT_DELAY_S = 60  # fallback when entity is unavailable
TICK_FLOOR_S = 2
TICK_CEILING_S = 300
```

Remove from `__all__`: `"DELAY_MIN_S"`, `"DELAY_MAX_S"`, `"DEFAULT_DELAY_S"`, `"TICK_FLOOR_S"`, `"TICK_CEILING_S"`.

- [ ] **Step 3: Run the full suite**

Run: `PYTHONPATH=. pytest -q -m "not docker_e2e"`
Expected: PASS (no ImportError for removed constants).

- [ ] **Step 4: Commit**

```bash
git add custom_components/sanitized_presence/const.py
git commit -m "refactor(const): drop dead pulse-model constants"
```

---

## Task 8: Wire the select suffix and controller in discovery.py

**Files:**
- Modify: `custom_components/sanitized_presence/discovery.py`
- Modify: `custom_components/sanitized_presence/tests/test_discovery.py`

- [ ] **Step 1: Write a failing test for the new required suffix**

Append to `tests/test_discovery.py` a test asserting a device missing the
`sensor` (select) entity is skipped. First read the existing test file to
match its fixture style:

Run: `PYTHONPATH=. pytest custom_components/sanitized_presence/tests/test_discovery.py -v` (see current passing tests and helpers)

Then add (adapting entity-building helpers to the file's existing style):

```python
def test_device_missing_sensor_select_is_skipped(hass):
    """A radar without a 'sensor' select entity is not adopted.

    Validates: recovery is impossible without the select entity, so such a
    device must be skipped like any other missing-required-entity case.
    Code: custom_components/sanitized_presence/discovery.py::SanitizedPresenceManager._resolve_entities
    Assertion: _resolve_entities returns None when the 'sensor' suffix has
        no matching entity.
    Method:
    1. Arrange: device with all required entities except 'sensor'.
    2. Act: call manager._resolve_entities(device).
    3. Assert: result is None.
    """
    # (build `device` with entities for every required suffix except
    #  SUFFIX_SENSOR, following the existing test helpers in this file)
    ...
```

> Note for the implementer: reuse the exact device/entity construction
> helpers already present in `test_discovery.py`; do not invent a new
> fixture shape. Include `unique_id` values ending in
> `_<suffix>_zigbee2mqtt` for each required suffix so `match_unique_id_suffix`
> resolves them.

- [ ] **Step 2: Run the test to verify it fails**

Run: `PYTHONPATH=. pytest custom_components/sanitized_presence/tests/test_discovery.py -k missing_sensor -v`
Expected: FAIL (sensor suffix not yet required; device resolves instead of returning None).

- [ ] **Step 3: Add SUFFIX_SENSOR to discovery and wire the controller**

In `discovery.py`:

1. Update imports — replace the pulse-era imports and add the new ones:

```python
from .binary_sensor import SanitizedPresenceBinarySensor
from .const import (
    CONF_POLL_INTERVAL,
    DEFAULT_POLL_S,
    SUFFIX_DETECTION_RANGE,
    SUFFIX_PRESENCE,
    SUFFIX_SENSOR,
    SUFFIX_SHIELD_RANGE,
    SUFFIX_TARGET_DISTANCE,
    TARGET_MODELS,
)
from .recovery import RecoveryController
from .sensor import StatusSensorEntity
```

2. Update `_REQUIRED_SUFFIXES` (drop `SUFFIX_DEPARTURE_DELAY`, add `SUFFIX_SENSOR`):

```python
_REQUIRED_SUFFIXES = (
    SUFFIX_TARGET_DISTANCE,
    SUFFIX_DETECTION_RANGE,
    SUFFIX_SHIELD_RANGE,
    SUFFIX_PRESENCE,
    SUFFIX_SENSOR,
)
```

3. In `_discover_and_add_sensors`, construct the controller and the new
   sensor pair (replace the `SanitizedPresenceBinarySensor(...)` and
   `DeadlineSensorEntity(...)` block):

```python
            eids = device.eids
            controller = RecoveryController(
                hass=self.hass,
                device_id=device.id,
                device_name=device.name,
                sensor_eid=eids[SUFFIX_SENSOR],
            )
            binary_sensor = SanitizedPresenceBinarySensor(
                hass=self.hass,
                entry=self.entry,
                device_id=device.id,
                device_name=device.name,
                device_identifiers=device.identifiers,
                target_distance_eid=eids[SUFFIX_TARGET_DISTANCE],
                detection_range_eid=eids[SUFFIX_DETECTION_RANGE],
                shield_range_eid=eids[SUFFIX_SHIELD_RANGE],
                presence_eid=eids[SUFFIX_PRESENCE],
                controller=controller,
            )
            status_sensor = StatusSensorEntity(
                hass=self.hass,
                entry=self.entry,
                device_id=device.id,
                device_name=device.name,
                device_identifiers=device.identifiers,
            )
            binary_sensor.set_status_sensor(status_sensor)
            self._sensors[device.id] = (binary_sensor, status_sensor)
            new_binary.append(binary_sensor)
            new_sensors.append(status_sensor)
```

4. Remove the now-unused `SUFFIX_DEPARTURE_DELAY` and `DeadlineSensorEntity`
   imports if still present.

- [ ] **Step 4: Run discovery tests to verify they pass**

Run: `PYTHONPATH=. pytest custom_components/sanitized_presence/tests/test_discovery.py -v`
Expected: PASS (including the new missing-sensor test).

- [ ] **Step 5: Remove SUFFIX_DEPARTURE_DELAY from const.py**

Now unreferenced outside tests. In `const.py` delete:

```python
SUFFIX_DEPARTURE_DELAY = "departure_delay"
```

and its `__all__` entry. Re-run `PYTHONPATH=. pytest -q -m "not docker_e2e"`; expected PASS.

- [ ] **Step 6: Commit**

```bash
git add custom_components/sanitized_presence/discovery.py custom_components/sanitized_presence/const.py custom_components/sanitized_presence/tests/test_discovery.py
git commit -m "feat(discovery): require select 'sensor' entity and wire RecoveryController"
```

---

## Task 9: Diagnostics & status-sensor integration test

**Files:**
- Modify: `custom_components/sanitized_presence/tests/test_recovery.py`

- [ ] **Step 1: Write a failing test for the diagnostics snapshot**

Append to `tests/test_recovery.py`:

```python
class TestDiagnostics:
    """diagnostics() reports the safety-rail state the status sensor shows."""

    def test_diagnostics_reports_cooldown_and_counts(self, monkeypatch):
        """After a reset, diagnostics shows cooldown-left and rate count.

        Validates: the status sensor receives accurate, current safety-rail
        figures (cooldown remaining, resets in the window).
        Code: custom_components/sanitized_presence/recovery.py::RecoveryController.diagnostics
        Assertion: immediately after a reset at t=1000, diagnostics(now=1000)
            shows cooldown_left==RESET_COOLDOWN_SEC and rate_window_count==1.
        Method:
        1. Arrange: controller; record a reset at t=1000.
        2. Act: diagnostics(now=1000).
        3. Assert: cooldown_left and rate_window_count as expected.
        """
        ctrl = _make_controller()
        ctrl._record_reset(1000.0)
        diag = ctrl.diagnostics(now=1000.0)
        assert diag["cooldown_left"] == RESET_COOLDOWN_SEC
        assert diag["rate_window_count"] == 1
        assert diag["resetting"] is False
```

- [ ] **Step 2: Run to verify it passes (diagnostics already implemented in Task 2)**

Run: `PYTHONPATH=. pytest custom_components/sanitized_presence/tests/test_recovery.py::TestDiagnostics -v`
Expected: PASS. (If it fails, fix `diagnostics()` to match.)

- [ ] **Step 3: Commit**

```bash
git add custom_components/sanitized_presence/tests/test_recovery.py
git commit -m "test(recovery): assert diagnostics snapshot fields"
```

---

## Task 10: Full suite, lint, and dead-code scan

**Files:** none (verification only).

- [ ] **Step 1: Run the full unit suite**

Run: `PYTHONPATH=. pytest -q -m "not docker_e2e"`
Expected: PASS, no errors. Note the test count.

- [ ] **Step 2: Run black and pylint**

Run: `black --check custom_components/sanitized_presence && pylint custom_components/sanitized_presence`
Expected: black clean; pylint 10/10 (matching the repo's prior bar). Fix any findings.

- [ ] **Step 3: Run vulture for dead code**

Run: `vulture custom_components/sanitized_presence` (use the repo's configured invocation if different — check `pyproject.toml`).
Expected: no dead code except known false positives. If `auto_reset.py` shows as unused, that confirms Task 11.

- [ ] **Step 4: Commit any formatting fixes**

```bash
git add -A
git commit -m "style: black/pylint fixes after recovery rewrite"
```

(Skip the commit if there were no changes.)

---

## Task 11: Remove vestigial auto_reset.py (APPROVAL GATE)

**Files:**
- Remove: `custom_components/sanitized_presence/auto_reset.py`
- Remove: `custom_components/sanitized_presence/tests/test_auto_reset.py`

> **STOP — do not delete without explicit user approval.** Per the spec and
> the global-rules audit, dead code is removed only after the user confirms.

- [ ] **Step 1: Prove it is unreferenced**

Run: `rg -n 'auto_reset|AutoResetBinarySensor' custom_components/sanitized_presence --glob '!auto_reset.py' --glob '!tests/test_auto_reset.py'`
Expected: no matches. Paste the (empty) result to the user.

- [ ] **Step 2: Ask the user to confirm removal**

Show the empty grep result and ask: "`auto_reset.py` is now unreferenced. Confirm removal?" Wait for explicit approval.

- [ ] **Step 3: On approval, remove the files**

```bash
git rm custom_components/sanitized_presence/auto_reset.py custom_components/sanitized_presence/tests/test_auto_reset.py
```

- [ ] **Step 4: Run the full suite**

Run: `PYTHONPATH=. pytest -q -m "not docker_e2e"`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git commit -m "refactor: remove vestigial AutoResetBinarySensor base"
```

---

## Task 12: Bump manifest version & release

**Files:**
- Modify: `custom_components/sanitized_presence/manifest.json`

- [ ] **Step 1: Bump the version**

Edit `manifest.json`, change `"version": "2026052601"` to today's stamp
(format `YYYYMMDDNN`, e.g. `"2026052901"`). HACS pulls from releases; without
a bump the change is reverted.

- [ ] **Step 2: Verify JSON is valid**

Run: `PYTHONPATH=. python -c "import json; print(json.load(open('custom_components/sanitized_presence/manifest.json'))['version'])"`
Expected: the new version string.

- [ ] **Step 3: Final full-suite run**

Run: `PYTHONPATH=. pytest -q -m "not docker_e2e"`
Expected: PASS.

- [ ] **Step 4: Commit and push (triggers release on main)**

```bash
git add custom_components/sanitized_presence/manifest.json
git commit -m "chore: bump manifest version for recovery state machine"
```

Push per the user's branching workflow (the release workflow triggers on a
`manifest.json` change pushed to `main`). Confirm with the user before
pushing to `main`.

---

## Post-Implementation (deployment — user-driven, not a code task)

After verification on the live fleet:

1. Deploy the integration to the HA host's
   `<config>/custom_components/sanitized_presence/`.
2. Restart HA core; confirm all device pairs adopt and report `mode=normal`.
3. **Delete** `<config>/pyscript/occupancy_reset.py` so the pyscript and the
   integration do not both drive the select entity (coexistence would cause
   competing reset cycles).

---

## Self-Review Notes

- **Spec coverage:** NORMAL mirror (Task 5), latch trigger (Task 5), health
  trigger (Task 5 `_on_health_tick`), RECOVERY in_range-only output (Task 5),
  reset cycle order+delays (Task 3), echo suppression (Task 5), exit on real
  off (Task 5), cooldown/rate-limit/circuit-breaker (Task 2), off-fallback
  (Task 4), error handling (Task 3), StatusSensorEntity (Task 6), discovery
  `sensor` suffix (Task 8), constants (Tasks 1/7/8), dead-code removal with
  approval gate (Task 11), manifest bump + pyscript removal (Task 12 + post).
  All spec sections map to a task.
- **Test discipline:** virtual time via explicit `now=` args (no wall-clock),
  `asyncio.sleep` patched, structured docstrings, exact `select_option` order
  asserted only because the sequence is the documented contract.
- **Type/name consistency:** `MODE_NORMAL`/`MODE_RECOVERY`, `RecoveryController`
  methods `request_reset`/`async_reset`/`maybe_recover_off`/`is_resetting`/
  `diagnostics`, `StatusSensorEntity.set_status`, and the `controller=` ctor
  arg are used consistently across tasks.
