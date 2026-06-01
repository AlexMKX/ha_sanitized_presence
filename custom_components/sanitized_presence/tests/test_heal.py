"""Behavior tests for HealController.

How to run:
    PYTHONPATH=. pytest custom_components/sanitized_presence/tests/test_heal.py
"""

from __future__ import annotations

# pylint: disable=protected-access

from unittest.mock import MagicMock

import custom_components.sanitized_presence.heal as heal_mod
from custom_components.sanitized_presence.heal import HealController


def _record_async_call(calls):
    async def _call(domain, service, data, blocking=False):
        calls.append((data["option"],))
    return _call


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
