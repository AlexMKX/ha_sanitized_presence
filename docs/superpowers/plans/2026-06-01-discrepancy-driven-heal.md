# Discrepancy-Driven Heal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the latch + periodic-health + distance-gating model with a discrepancy-driven heal: only toggle a radar that is demonstrably stuck (`presence=on` while `distance` out of range for 15 min), judge the result by observation, bound the effort, and freeze the sanitized output during the heal.

**Architecture:** A thin entity (`SanitizedPresenceBinarySensor`) owns the NORMAL/HEALING/BACKOFF state, discrepancy tracking and output freeze; it delegates the confirmed `off->on` toggle + 60 s observe loop (with occupied/empty stop conditions and an iteration cap) to a rewritten `HealController` (new module `heal.py`, replacing `recovery.py`). `distance` is used only for detection and stop conditions, never as the output value.

**Tech Stack:** Home Assistant custom component (Python 3.12), pytest + pytest-asyncio (`asyncio_mode=auto`), MagicMock-based unit tests, pylint, black, vulture.

**Spec:** `docs/superpowers/specs/2026-06-01-discrepancy-driven-heal-design.md`

**Test command (from repo root):** `PYTHONPATH=. pytest -q -m "not docker_e2e"`

---

## File Structure

- `custom_components/sanitized_presence/const.py` — swap constants (add heal set, remove obsolete reset/latch/health set).
- `custom_components/sanitized_presence/heal.py` — NEW. `HealController`: confirmed toggle, observe loop, iteration cap, park-fallback, diagnostics. Replaces `recovery.py`.
- `custom_components/sanitized_presence/recovery.py` — DELETE.
- `custom_components/sanitized_presence/binary_sensor.py` — rewrite state machine.
- `custom_components/sanitized_presence/discovery.py` — import/construct `HealController`; entity binds observers.
- `custom_components/sanitized_presence/sensor.py` — unchanged (`StatusSensorEntity.set_status(mode, diag)` is generic).
- `custom_components/sanitized_presence/tests/test_heal.py` — NEW (replaces `test_recovery.py`).
- `custom_components/sanitized_presence/tests/test_recovery.py` — DELETE.
- `custom_components/sanitized_presence/tests/test_binary_sensor.py` — rewrite for new state machine.
- `custom_components/sanitized_presence/tests/test_discovery.py` — update import/construction.
- `custom_components/sanitized_presence/manifest.json` — version bump.

**Controller API (target):**

```python
class HealController:
    def __init__(self, hass, device_id, device_name, sensor_eid) -> None
    def bind_observers(self, presence_on: Callable[[], bool], in_range_now: Callable[[], bool]) -> None
    @property
    def is_healing(self) -> bool
    async def async_heal(self) -> str            # "occupied" | "empty" | "gave_up"
    async def _confirmed_toggle(self) -> bool     # True if both off and on confirmed
    async def _observe(self) -> str | None        # "occupied" | "empty" | None
    async def maybe_recover_parked(self) -> None
    def diagnostics(self) -> dict                 # {"healing": bool, "iteration": int}
```

Timing uses module-level `time.monotonic()` and `asyncio.sleep(...)`; tests monkeypatch both (`heal.time.monotonic`, `heal.asyncio.sleep`) for determinism — matching the existing pattern in `test_recovery.py`.

---

## Task 1: Constants

**Files:**
- Modify: `custom_components/sanitized_presence/const.py`

- [ ] **Step 1: Replace the recovery-era constants with the heal set**

In `const.py`, locate the block from `# --- Recovery state machine ---` through the `OFF_FALLBACK_INTERVAL_SEC` definition and the `SUFFIX_SENSOR` line. Replace the recovery/reset constants (keep `SHIELD_FLOOR_M`, `SUFFIX_SENSOR`, and all `SUFFIX_*` / `TARGET_MODELS` / poll / domain constants) so the file contains exactly these heal constants:

```python
# --- Discrepancy-driven heal ---

# Enter heal when presence has been "on" while distance is out of range for
# this long (continuous). A still person may briefly show distance=0, but
# not for this duration; a stuck radar holds the discrepancy indefinitely.
DISCREPANCY_TRIGGER_SEC = 900

# Observation window after each toggle: how long we watch presence/distance
# for a stop condition before toggling again.
HEAL_OBSERVE_SEC = 60

# Consecutive iterations (toggle + observe) before giving up and backing off.
HEAL_MAX_ITERATIONS = 5

# Pause after giving up before heal may re-enter.
HEAL_BACKOFF_SEC = 1800

# Wait this long for the select to confirm 'off' / 'on' (Z2M optimistic=off).
# A timeout means the cluster write did not reach the radar.
HEAL_CMD_TIMEOUT_SEC = 10

# Stop A requires in_range AND presence=on sustained this long (debounce
# against a single-sample fluctuation).
STOP_A_CONFIRM_SEC = 5

# Internal poll cadence for confirm/observe loops.
HEAL_POLL_SEC = 0.5

# Park-fallback: restore a select parked at any non-'on' value to 'on'.
PARK_FALLBACK_INTERVAL_SEC = 60

# Select-walk: simple off->on toggle (no intermediate 'unoccupied').
SENSOR_RESET_SEQUENCE = ("off", "on")
```

Ensure `SHIELD_FLOOR_M = 0.1` remains. Remove these now-obsolete names entirely (and their `__all__` entries if present): `RECOVERY_PRESENCE_ON_SEC`, `HEALTH_RESET_INTERVAL_SEC`, `SENSOR_PHASE_DELAY_SEC`, `RESET_COOLDOWN_SEC`, `RESET_RATE_WINDOW_SEC`, `RESET_RATE_LIMIT`, `RESET_RATE_BLOCK_SEC`, `OFF_FALLBACK_INTERVAL_SEC`.

Update `__all__` to list the new constants plus the retained ones (`DOMAIN`, `CONF_POLL_INTERVAL`, `DEFAULT_POLL_S`, `POLL_MIN_S`, `POLL_MAX_S`, `PLATFORMS`, `TARGET_MODELS`, all `SUFFIX_*`, `SHIELD_FLOOR_M`, and every new heal constant + `SENSOR_RESET_SEQUENCE`).

- [ ] **Step 2: Sanity import**

Run: `PYTHONPATH=. python -c "from custom_components.sanitized_presence import const as c; print(c.DISCREPANCY_TRIGGER_SEC, c.HEAL_OBSERVE_SEC, c.SENSOR_RESET_SEQUENCE); assert not hasattr(c, 'HEALTH_RESET_INTERVAL_SEC')"`
Expected: prints `900 60 ('off', 'on')`, no AssertionError.

- [ ] **Step 3: Commit**

```bash
git add custom_components/sanitized_presence/const.py
git commit -m "feat: heal constants; drop recovery/latch/rate-limit constants"
```

---

## Task 2: HealController skeleton

**Files:**
- Create: `custom_components/sanitized_presence/heal.py`
- Create: `custom_components/sanitized_presence/tests/test_heal.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_heal.py`:

```python
"""Behavior tests for HealController.

How to run:
    PYTHONPATH=. pytest custom_components/sanitized_presence/tests/test_heal.py
"""

from __future__ import annotations

# pylint: disable=protected-access

from unittest.mock import MagicMock

import custom_components.sanitized_presence.heal as heal_mod
from custom_components.sanitized_presence.heal import HealController


def _make_controller(hass=None, present=True, in_range=True):
    ctrl = HealController(
        hass=hass or MagicMock(),
        device_id="dev1",
        device_name="Radar 1",
        sensor_eid="select.radar_sensor",
    )
    ctrl.bind_observers(presence_on=lambda: present, in_range_now=lambda: in_range)
    return ctrl


class TestSkeleton:
    """Construction, gate and diagnostics."""

    def test_not_healing_initially(self):
        """A fresh controller is not healing.

        Code: heal.py::HealController.is_healing
        """
        ctrl = _make_controller()
        assert ctrl.is_healing is False

    def test_diagnostics_fields(self):
        """diagnostics() exposes healing and iteration.

        Code: heal.py::HealController.diagnostics
        """
        ctrl = _make_controller()
        d = ctrl.diagnostics()
        assert d == {"healing": False, "iteration": 0}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. pytest custom_components/sanitized_presence/tests/test_heal.py -q`
Expected: FAIL — `ModuleNotFoundError: ... heal`.

- [ ] **Step 3: Write minimal implementation**

Create `heal.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=. pytest custom_components/sanitized_presence/tests/test_heal.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add custom_components/sanitized_presence/heal.py custom_components/sanitized_presence/tests/test_heal.py
git commit -m "feat: HealController skeleton (gate + diagnostics)"
```

---

## Task 3: Confirmed toggle

**Files:**
- Modify: `custom_components/sanitized_presence/heal.py`
- Test: `custom_components/sanitized_presence/tests/test_heal.py`

- [ ] **Step 1: Write the failing tests**

Append to `test_heal.py`:

```python
import pytest


def _patch_clock(monkeypatch):
    """Replace heal.time.monotonic + heal.asyncio.sleep with a fake clock.

    Returns a dict with 'now'; sleep advances it instantly.
    """
    state = {"now": 0.0}
    monkeypatch.setattr(heal_mod.time, "monotonic", lambda: state["now"])

    async def fake_sleep(sec):
        state["now"] += sec

    monkeypatch.setattr(heal_mod.asyncio, "sleep", fake_sleep)
    return state


def _select_state(value):
    s = MagicMock()
    s.state = value
    return s


class TestConfirmedToggle:
    """off -> confirm -> on -> confirm, with timeout detection."""

    @pytest.mark.asyncio
    async def test_both_confirm_happy_path(self, monkeypatch):
        """Toggle confirms when the select reports off then on.

        Code: heal.py::HealController._confirmed_toggle
        Assertion: returns True; select_option called off then on.
        """
        _patch_clock(monkeypatch)
        hass = MagicMock()
        # select reads: first poll after 'off' returns off; after 'on' returns on
        seq = iter(["off", "off", "on", "on"])
        hass.states.get = lambda eid: _select_state(next(seq, "on"))
        calls = []
        hass.services.async_call = _record_async_call(calls)
        ctrl = _make_controller(hass)
        ok = await ctrl._confirmed_toggle()
        assert ok is True
        assert calls == [("off",), ("on",)]

    @pytest.mark.asyncio
    async def test_off_confirm_timeout(self, monkeypatch):
        """If 'off' never confirms within timeout, returns False but still sends 'on'.

        Code: heal.py::HealController._confirmed_toggle
        Assertion: returns False; both off and on still attempted.
        """
        _patch_clock(monkeypatch)
        hass = MagicMock()
        hass.states.get = lambda eid: _select_state("on")  # never goes off
        calls = []
        hass.services.async_call = _record_async_call(calls)
        ctrl = _make_controller(hass)
        ok = await ctrl._confirmed_toggle()
        assert ok is False
        assert ("off",) in calls and ("on",) in calls
```

Add this helper near the top of the file (after imports):

```python
def _record_async_call(calls):
    async def _call(domain, service, data, blocking=False):
        calls.append((data["option"],))
    return _call
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=. pytest custom_components/sanitized_presence/tests/test_heal.py::TestConfirmedToggle -q`
Expected: FAIL — `_confirmed_toggle` does not exist.

- [ ] **Step 3: Implement**

Add to `HealController` in `heal.py`:

```python
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
```

- [ ] **Step 4: Run to verify pass**

Run: `PYTHONPATH=. pytest custom_components/sanitized_presence/tests/test_heal.py::TestConfirmedToggle -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add custom_components/sanitized_presence/heal.py custom_components/sanitized_presence/tests/test_heal.py
git commit -m "feat: HealController confirmed off->on toggle with timeout detection"
```

---

## Task 4: Observe loop (Stop A / Stop B)

**Files:**
- Modify: `custom_components/sanitized_presence/heal.py`
- Test: `custom_components/sanitized_presence/tests/test_heal.py`

- [ ] **Step 1: Write the failing tests**

Append to `test_heal.py`:

```python
class TestObserve:
    """Stop A (sustained in_range+present), Stop B (whole window empty)."""

    @pytest.mark.asyncio
    async def test_stop_a_sustained_occupied(self, monkeypatch):
        """in_range+present sustained STOP_A_CONFIRM_SEC -> 'occupied'.

        Code: heal.py::HealController._observe
        """
        _patch_clock(monkeypatch)
        ctrl = _make_controller(present=True, in_range=True)
        assert await ctrl._observe() == "occupied"

    @pytest.mark.asyncio
    async def test_stop_a_needs_sustain(self, monkeypatch):
        """A single in_range sample shorter than STOP_A_CONFIRM_SEC is not Stop A.

        Code: heal.py::HealController._observe
        Method: in_range true only on the very first sample, then false; with
        presence on but distance out of range the rest of the window -> not
        occupied; presence stays on so not empty -> None (iterate).
        """
        _patch_clock(monkeypatch)
        from custom_components.sanitized_presence.const import HEAL_POLL_SEC
        flags = {"n": 0}

        def in_range_now():
            flags["n"] += 1
            return flags["n"] <= 1  # in range only first sample

        ctrl = _make_controller(present=True)
        ctrl.bind_observers(presence_on=lambda: True, in_range_now=in_range_now)
        assert await ctrl._observe() is None

    @pytest.mark.asyncio
    async def test_stop_b_whole_window_empty(self, monkeypatch):
        """presence off for the entire window -> 'empty'.

        Code: heal.py::HealController._observe
        """
        _patch_clock(monkeypatch)
        ctrl = _make_controller(present=False, in_range=False)
        assert await ctrl._observe() == "empty"

    @pytest.mark.asyncio
    async def test_observe_neither(self, monkeypatch):
        """presence on but never in range -> None (needs another iteration).

        Code: heal.py::HealController._observe
        """
        _patch_clock(monkeypatch)
        ctrl = _make_controller(present=True, in_range=False)
        assert await ctrl._observe() is None
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=. pytest custom_components/sanitized_presence/tests/test_heal.py::TestObserve -q`
Expected: FAIL — `_observe` does not exist.

- [ ] **Step 3: Implement**

Add to `HealController`:

```python
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
```

- [ ] **Step 4: Run to verify pass**

Run: `PYTHONPATH=. pytest custom_components/sanitized_presence/tests/test_heal.py::TestObserve -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add custom_components/sanitized_presence/heal.py custom_components/sanitized_presence/tests/test_heal.py
git commit -m "feat: HealController observe loop with Stop A/B conditions"
```

---

## Task 5: async_heal loop + iteration cap

**Files:**
- Modify: `custom_components/sanitized_presence/heal.py`
- Test: `custom_components/sanitized_presence/tests/test_heal.py`

- [ ] **Step 1: Write the failing tests**

Append to `test_heal.py`:

```python
class TestAsyncHeal:
    """The bounded iterate-until-resolved loop."""

    @pytest.mark.asyncio
    async def test_resolves_occupied_first_iteration(self, monkeypatch):
        """Stop A on the first observe -> 'occupied'; one toggle.

        Code: heal.py::HealController.async_heal
        """
        _patch_clock(monkeypatch)
        hass = MagicMock()
        hass.states.get = lambda eid: _select_state("off")  # toggle won't confirm; irrelevant
        hass.services.async_call = _record_async_call([])
        ctrl = _make_controller(hass, present=True, in_range=True)
        result = await ctrl.async_heal()
        assert result == "occupied"
        assert ctrl.is_healing is False

    @pytest.mark.asyncio
    async def test_resolves_empty(self, monkeypatch):
        """Whole window empty -> 'empty'.

        Code: heal.py::HealController.async_heal
        """
        _patch_clock(monkeypatch)
        hass = MagicMock()
        hass.states.get = lambda eid: _select_state("on")
        hass.services.async_call = _record_async_call([])
        ctrl = _make_controller(hass, present=False, in_range=False)
        assert await ctrl.async_heal() == "empty"

    @pytest.mark.asyncio
    async def test_gives_up_after_max_iterations(self, monkeypatch):
        """Never resolving -> 'gave_up' after HEAL_MAX_ITERATIONS toggles.

        Code: heal.py::HealController.async_heal
        """
        _patch_clock(monkeypatch)
        from custom_components.sanitized_presence.const import HEAL_MAX_ITERATIONS
        hass = MagicMock()
        hass.states.get = lambda eid: _select_state("on")
        toggles = []
        hass.services.async_call = _record_async_call(toggles)
        ctrl = _make_controller(hass, present=True, in_range=False)  # neither A nor B
        result = await ctrl.async_heal()
        assert result == "gave_up"
        # each iteration toggles off+on => 2 calls per iteration
        assert len(toggles) == HEAL_MAX_ITERATIONS * 2
        assert ctrl.is_healing is False
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=. pytest custom_components/sanitized_presence/tests/test_heal.py::TestAsyncHeal -q`
Expected: FAIL — `async_heal` not defined.

- [ ] **Step 3: Implement**

Add to `HealController` (import `HEAL_MAX_ITERATIONS` is already imported in Task 2):

```python
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
```

- [ ] **Step 4: Run to verify pass**

Run: `PYTHONPATH=. pytest custom_components/sanitized_presence/tests/test_heal.py::TestAsyncHeal -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add custom_components/sanitized_presence/heal.py custom_components/sanitized_presence/tests/test_heal.py
git commit -m "feat: HealController.async_heal iterate-until-resolved with cap"
```

---

## Task 6: Park-fallback + diagnostics during heal

**Files:**
- Modify: `custom_components/sanitized_presence/heal.py`
- Test: `custom_components/sanitized_presence/tests/test_heal.py`

- [ ] **Step 1: Write the failing tests**

Append to `test_heal.py`:

```python
class TestParkFallback:
    """Restore a select parked at a non-'on' value when not healing."""

    @pytest.mark.asyncio
    async def test_restores_parked_off(self):
        """select parked 'off', not healing -> sends 'on'.

        Code: heal.py::HealController.maybe_recover_parked
        """
        hass = MagicMock()
        hass.states.get = lambda eid: _select_state("off")
        calls = []
        hass.services.async_call = _record_async_call(calls)
        ctrl = _make_controller(hass)
        await ctrl.maybe_recover_parked()
        assert calls == [("on",)]

    @pytest.mark.asyncio
    async def test_noop_when_on(self):
        """select already 'on' -> no service call.

        Code: heal.py::HealController.maybe_recover_parked
        """
        hass = MagicMock()
        hass.states.get = lambda eid: _select_state("on")
        calls = []
        hass.services.async_call = _record_async_call(calls)
        ctrl = _make_controller(hass)
        await ctrl.maybe_recover_parked()
        assert calls == []

    @pytest.mark.asyncio
    async def test_ignores_unavailable(self):
        """select 'unavailable' -> no service call.

        Code: heal.py::HealController.maybe_recover_parked
        """
        hass = MagicMock()
        hass.states.get = lambda eid: _select_state("unavailable")
        calls = []
        hass.services.async_call = _record_async_call(calls)
        ctrl = _make_controller(hass)
        await ctrl.maybe_recover_parked()
        assert calls == []

    @pytest.mark.asyncio
    async def test_guarded_while_healing(self):
        """No park action while a heal run is in flight.

        Code: heal.py::HealController.maybe_recover_parked
        """
        hass = MagicMock()
        hass.states.get = lambda eid: _select_state("off")
        calls = []
        hass.services.async_call = _record_async_call(calls)
        ctrl = _make_controller(hass)
        ctrl._healing = True
        await ctrl.maybe_recover_parked()
        assert calls == []
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=. pytest custom_components/sanitized_presence/tests/test_heal.py::TestParkFallback -q`
Expected: FAIL — `maybe_recover_parked` not defined.

- [ ] **Step 3: Implement**

Add to `HealController`:

```python
    async def maybe_recover_parked(self) -> None:
        """Restore a select parked at any non-'on' value to 'on'.

        A select parked at 'off'/'unoccupied' with no heal running means a
        heal toggle was interrupted (e.g. HA restart). Ignores
        'unknown'/'unavailable' so an offline device is not whipsawed, and
        is guarded so it never fights an in-flight heal toggle.
        """
        if self._healing:
            return
        st = self.hass.states.get(self._sensor_eid)
        if st is None or st.state in ("on", *_IGNORED_SELECT):
            return
        _LOGGER.info(
            "sanitized_presence: %s park-fallback — select parked %r, restoring 'on'",
            self._device_name,
            st.state,
        )
        await self._select_option("on")
```

- [ ] **Step 4: Run to verify pass**

Run: `PYTHONPATH=. pytest custom_components/sanitized_presence/tests/test_heal.py -q`
Expected: PASS (all heal tests).

- [ ] **Step 5: Commit**

```bash
git add custom_components/sanitized_presence/heal.py custom_components/sanitized_presence/tests/test_heal.py
git commit -m "feat: HealController park-fallback"
```

---

## Task 7: Binary sensor state machine rewrite

**Files:**
- Modify (rewrite): `custom_components/sanitized_presence/binary_sensor.py`
- Modify (rewrite): `custom_components/sanitized_presence/tests/test_binary_sensor.py`

This task replaces the NORMAL/RECOVERY machine with NORMAL/HEALING/BACKOFF + discrepancy tracking. It is split into sub-steps but committed once at the end.

- [ ] **Step 1: Write the failing tests**

Replace the entire contents of `tests/test_binary_sensor.py` with:

```python
"""Behavior tests for SanitizedPresenceBinarySensor (discrepancy-driven heal).

How to run:
    PYTHONPATH=. pytest custom_components/sanitized_presence/tests/test_binary_sensor.py
"""

from __future__ import annotations

# pylint: disable=protected-access

from unittest.mock import MagicMock

from custom_components.sanitized_presence.binary_sensor import (
    STATE_BACKOFF,
    STATE_HEALING,
    STATE_NORMAL,
    SanitizedPresenceBinarySensor,
)
from custom_components.sanitized_presence.const import DISCREPANCY_TRIGGER_SEC


def _state(value):
    s = MagicMock()
    s.state = value
    return s


def _make_sensor(hass=None, controller=None):
    hass = hass or MagicMock()
    controller = controller or MagicMock()
    controller.is_healing = False
    controller.diagnostics = MagicMock(return_value={"healing": False, "iteration": 0})
    sensor = SanitizedPresenceBinarySensor(
        hass=hass,
        entry=MagicMock(),
        device_id="dev1",
        device_name="Radar 1",
        device_identifiers={("zigbee2mqtt", "dev1")},
        target_distance_eid="sensor.d",
        detection_range_eid="number.det",
        shield_range_eid="number.sh",
        presence_eid="binary_sensor.p",
        controller=controller,
    )
    return sensor


def _set_states(hass, presence="off", distance=0.0, detect=5.0, shield=0.0):
    table = {
        "binary_sensor.p": _state(presence),
        "sensor.d": _state(distance),
        "number.det": _state(detect),
        "number.sh": _state(shield),
        "select.s": _state("on"),
    }
    hass.states.get = lambda eid: table.get(eid)
    return table


class TestNormalMirror:
    def test_mirrors_presence_on(self):
        """NORMAL output mirrors presence=on.

        Code: binary_sensor.py::SanitizedPresenceBinarySensor._recompute
        """
        s = _make_sensor()
        _set_states(s.hass, presence="on", distance=2.0)
        s._recompute(now=100.0)
        assert s._attr_is_on is True
        assert s._state == STATE_NORMAL

    def test_mirrors_presence_off(self):
        """NORMAL output mirrors presence=off.

        Code: binary_sensor.py::SanitizedPresenceBinarySensor._recompute
        """
        s = _make_sensor()
        _set_states(s.hass, presence="off")
        s._recompute(now=100.0)
        assert s._attr_is_on is False


class TestDiscrepancyTrigger:
    def test_discrepancy_accumulates_and_triggers(self):
        """presence=on + out-of-range for >= DISCREPANCY_TRIGGER_SEC enters HEALING.

        Code: binary_sensor.py::SanitizedPresenceBinarySensor._recompute / _enter_heal
        """
        s = _make_sensor()
        _set_states(s.hass, presence="on", distance=0.0)  # 0 => not in range
        s._recompute(now=0.0)
        assert s._state == STATE_NORMAL  # just started accumulating
        s._recompute(now=float(DISCREPANCY_TRIGGER_SEC))
        assert s._state == STATE_HEALING
        s.hass.async_create_task.assert_called()

    def test_in_range_clears_discrepancy(self):
        """A distance in range resets the discrepancy timer (no trigger).

        Code: binary_sensor.py::SanitizedPresenceBinarySensor._recompute
        """
        s = _make_sensor()
        _set_states(s.hass, presence="on", distance=0.0)
        s._recompute(now=0.0)
        _set_states(s.hass, presence="on", distance=2.0)  # in range
        s._recompute(now=10.0)
        _set_states(s.hass, presence="on", distance=0.0)
        s._recompute(now=float(DISCREPANCY_TRIGGER_SEC))  # only ~T-10 of new accрumulation
        assert s._state == STATE_NORMAL

    def test_presence_off_clears_discrepancy(self):
        """presence going off resets the discrepancy timer.

        Code: binary_sensor.py::SanitizedPresenceBinarySensor._recompute
        """
        s = _make_sensor()
        _set_states(s.hass, presence="on", distance=0.0)
        s._recompute(now=0.0)
        _set_states(s.hass, presence="off", distance=0.0)
        s._recompute(now=10.0)
        _set_states(s.hass, presence="on", distance=0.0)
        s._recompute(now=float(DISCREPANCY_TRIGGER_SEC))
        assert s._state == STATE_NORMAL


class TestFreezeDuringHeal:
    def test_output_frozen_while_healing(self):
        """While HEALING, output does not change with presence/distance.

        Code: binary_sensor.py::SanitizedPresenceBinarySensor._recompute
        """
        s = _make_sensor()
        s._state = STATE_HEALING
        s._attr_is_on = True
        _set_states(s.hass, presence="off", distance=0.0)
        s._recompute(now=100.0)
        assert s._attr_is_on is True  # frozen


class TestApplyHealResult:
    def test_occupied_sets_on_and_normal(self):
        """Heal result 'occupied' -> output on, state NORMAL, discrepancy cleared.

        Code: binary_sensor.py::SanitizedPresenceBinarySensor._apply_heal_result
        """
        s = _make_sensor()
        s._state = STATE_HEALING
        s._apply_heal_result("occupied", now=100.0)
        assert s._attr_is_on is True
        assert s._state == STATE_NORMAL
        assert s._discrepancy_since is None

    def test_empty_sets_off_and_normal(self):
        """Heal result 'empty' -> output off, state NORMAL.

        Code: binary_sensor.py::SanitizedPresenceBinarySensor._apply_heal_result
        """
        s = _make_sensor()
        s._state = STATE_HEALING
        s._attr_is_on = True
        s._apply_heal_result("empty", now=100.0)
        assert s._attr_is_on is False
        assert s._state == STATE_NORMAL

    def test_gave_up_enters_backoff(self):
        """Heal result 'gave_up' -> state BACKOFF with a future backoff_until.

        Code: binary_sensor.py::SanitizedPresenceBinarySensor._apply_heal_result
        """
        s = _make_sensor()
        s._state = STATE_HEALING
        s._apply_heal_result("gave_up", now=100.0)
        assert s._state == STATE_BACKOFF
        assert s._backoff_until > 100.0


class TestBackoffGate:
    def test_no_heal_during_backoff(self):
        """Discrepancy met but inside backoff -> stays out of HEALING.

        Code: binary_sensor.py::SanitizedPresenceBinarySensor._recompute
        """
        s = _make_sensor()
        s._state = STATE_BACKOFF
        s._backoff_until = 10_000.0
        s._discrepancy_since = 0.0
        _set_states(s.hass, presence="on", distance=0.0)
        s._recompute(now=float(DISCREPANCY_TRIGGER_SEC))
        assert s._state == STATE_BACKOFF
        s.hass.async_create_task.assert_not_called()

    def test_reenters_heal_after_backoff(self):
        """After backoff expiry with persistent discrepancy -> HEALING.

        Code: binary_sensor.py::SanitizedPresenceBinarySensor._recompute
        """
        s = _make_sensor()
        s._state = STATE_BACKOFF
        s._backoff_until = 50.0
        s._discrepancy_since = 0.0
        _set_states(s.hass, presence="on", distance=0.0)
        s._recompute(now=float(DISCREPANCY_TRIGGER_SEC))  # > backoff_until
        assert s._state == STATE_HEALING
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=. pytest custom_components/sanitized_presence/tests/test_binary_sensor.py -q`
Expected: FAIL — `STATE_HEALING` / new API not present.

- [ ] **Step 3: Rewrite `binary_sensor.py`**

Replace the entire contents of `binary_sensor.py` with:

```python
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
```

- [ ] **Step 4: Run the binary-sensor tests**

Run: `PYTHONPATH=. pytest custom_components/sanitized_presence/tests/test_binary_sensor.py -q`
Expected: PASS (all). If `test_in_range_clears_discrepancy` is brittle, confirm the timing math: after re-arming at t=10 with distance 0, at t=900 the age is 890 < 900 so no trigger — keep as is.

- [ ] **Step 5: Commit**

```bash
git add custom_components/sanitized_presence/binary_sensor.py custom_components/sanitized_presence/tests/test_binary_sensor.py
git commit -m "feat: discrepancy-driven heal state machine (NORMAL/HEALING/BACKOFF)"
```

---

## Task 8: Wire discovery to HealController; delete recovery module

**Files:**
- Modify: `custom_components/sanitized_presence/discovery.py:17,28,179-184`
- Delete: `custom_components/sanitized_presence/recovery.py`
- Delete: `custom_components/sanitized_presence/tests/test_recovery.py`
- Modify: `custom_components/sanitized_presence/tests/test_discovery.py` (import only, if it references RecoveryController)

- [ ] **Step 1: Update the import in discovery.py**

In `discovery.py`, change line 28 from:

```python
from .recovery import RecoveryController
```

to:

```python
from .heal import HealController
```

- [ ] **Step 2: Update construction in `_discover_and_add_sensors`**

Replace the `controller = RecoveryController(...)` block (around lines 179-184) with:

```python
            controller = HealController(
                hass=self.hass,
                device_id=device.id,
                device_name=device.name,
                sensor_eid=eids[SUFFIX_SENSOR],
            )
```

(The entity binds observers in its `__init__`, so no further wiring is needed here.)

- [ ] **Step 3: Delete the old module and its test; fix any stale import**

```bash
git rm custom_components/sanitized_presence/recovery.py
git rm custom_components/sanitized_presence/tests/test_recovery.py
grep -rn "RecoveryController\|from .recovery\|import recovery" custom_components/ || echo "no stale references"
```

If `test_discovery.py` imports `RecoveryController` or asserts on it, update those references to `HealController` (from `custom_components.sanitized_presence.heal`). The discovery flow tests should otherwise be unaffected (same constructor arity: hass/device_id/device_name/sensor_eid).

- [ ] **Step 4: Run discovery + full module import**

Run: `PYTHONPATH=. pytest custom_components/sanitized_presence/tests/test_discovery.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: wire discovery to HealController; remove recovery module"
```

---

## Task 9: Full verification + anti-bloat

**Files:**
- (verification only; fix any fallout in the files above)

- [ ] **Step 1: Full suite**

Run: `PYTHONPATH=. pytest -q -m "not docker_e2e"`
Expected: all PASS. Investigate and fix any failure at root cause (e.g. `test_integration_e2e.py` or `test_status_sensor.py` referencing the old `mode`/`set_status` shape — `set_status(state, diag)` is unchanged, so they should pass; if `test_status_sensor.py` asserts specific diagnostic keys from the old controller, update them to the new keys `healing`/`iteration`).

- [ ] **Step 2: Lint**

Run: `PYTHONPATH=. pylint custom_components/sanitized_presence`
Expected: 10.00/10. Fix import ordering (`asyncio`/`contextlib` stdlib; `voluptuous` third-party in heal.py) and any unused-import warnings. Do not add new disables unless genuinely required.

- [ ] **Step 3: Format**

Run: `black --check custom_components/sanitized_presence`
If not clean: `black custom_components/sanitized_presence`.

- [ ] **Step 4: Dead-code scan**

Run: `vulture custom_components/sanitized_presence --min-confidence 80 || true`
Expected: no references to removed constants (`RESET_*`, `HEALTH_RESET_INTERVAL_SEC`, `SENSOR_PHASE_DELAY_SEC`, `OFF_FALLBACK_INTERVAL_SEC`, `RECOVERY_PRESENCE_ON_SEC`) and no leftover `RecoveryController`. Confirm:

```bash
grep -rn "RECOVERY_PRESENCE_ON_SEC\|HEALTH_RESET_INTERVAL_SEC\|SENSOR_PHASE_DELAY_SEC\|RESET_COOLDOWN_SEC\|RESET_RATE_\|OFF_FALLBACK_INTERVAL_SEC\|RecoveryController" custom_components/ || echo "clean"
```
Expected: `clean`.

- [ ] **Step 5: Commit (if any fixes were made)**

```bash
git add -A
git commit -m "test: align suite with heal model; lint/format clean"
```

---

## Task 10: Version bump

**Files:**
- Modify: `custom_components/sanitized_presence/manifest.json`

- [ ] **Step 1: Bump version**

Set `"version"` in `manifest.json` to `"2026060101"`.

- [ ] **Step 2: Verify suite still green**

Run: `PYTHONPATH=. pytest -q -m "not docker_e2e"`
Expected: all PASS.

- [ ] **Step 3: Commit**

```bash
git add custom_components/sanitized_presence/manifest.json
git commit -m "chore: bump version to 2026060101 (discrepancy-driven heal)"
```

---

## Task 11: Release, deploy, verify on host

**Files:** none (operational).

- [ ] **Step 1: Push and watch release**

```bash
git push origin main
gh run list --workflow=release.yml --limit 1
```
Then `gh run watch <id>`; confirm `gh release view v2026060101`.

- [ ] **Step 2: Deploy via tarball**

Token at `/root/.ssh/environment` on the host (load with `export $(cat /root/.ssh/environment | xargs)`):

```bash
ssh root@hassio.h.xxl.cx 'set -e
  cd /tmp
  curl -sSL -o sp.tar.gz https://github.com/AlexMKX/ha_sanitized_presence/archive/refs/tags/v2026060101.tar.gz
  tar xzf sp.tar.gz
  rm -rf /config/custom_components/sanitized_presence
  cp -r ha_sanitized_presence-2026060101/custom_components/sanitized_presence /config/custom_components/
  rm -rf sp.tar.gz ha_sanitized_presence-2026060101
  grep version /config/custom_components/sanitized_presence/manifest.json
  ha core restart'
ssh root@hassio.h.xxl.cx 'until ha core info >/dev/null 2>&1; do sleep 5; done; echo UP'
```

- [ ] **Step 3: Verify on host**

```bash
ssh root@hassio.h.xxl.cx 'grep -i sanitized_presence /config/home-assistant.log | tail -30'
```
Expected: clean load, no tracebacks. Confirm all status sensors report `state=normal` shortly after startup (HEALING only on a sustained 15-min discrepancy). Note: full heal behavior (discrepancy -> heal -> occupied/empty/backoff) can only be observed over time on a genuinely stuck radar; document this as a runtime-monitoring item, not a startup check.

- [ ] **Step 4: Prerequisite reminder**

Confirm Z2M `optimistic` remains disabled for the radar `sensor` selects (deployment prerequisite from the spec). The confirmed-toggle timeout detection depends on it; with it on, heal still works but loses the "command did not land" signal.

---

## Self-Review (completed by plan author)

- **Spec coverage:** output model (Task 7 readers + freeze + mirror); discrepancy trigger (Task 7 TestDiscrepancyTrigger); confirmed toggle (Task 3); observe Stop A/B (Task 4); iteration cap + backoff (Task 5 + Task 7 TestApplyHealResult/TestBackoffGate); park-fallback (Task 6); diagnostics (Task 7 `_notify_status`); constants add/remove (Task 1); removal of distance-gating/health-tick/recovery module (Tasks 7, 8); prerequisites/degradation (Task 11 Step 4 + heal `_wait_select` docstring). All covered.
- **Placeholder scan:** every code step contains complete code; no TBD/TODO.
- **Type consistency:** `async_heal()` returns `"occupied"|"empty"|"gave_up"`; entity `_apply_heal_result` branches on those exact strings; states `STATE_NORMAL/HEALING/BACKOFF` used consistently; controller `bind_observers(presence_on, in_range_now)` matches entity `_read_presence_on`/`_read_in_range`; `diagnostics()` returns `{"healing","iteration"}` merged by entity `_notify_status`.
