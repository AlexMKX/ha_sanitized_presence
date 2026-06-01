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
