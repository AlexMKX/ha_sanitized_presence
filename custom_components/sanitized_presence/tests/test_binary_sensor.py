"""Behavior tests for SanitizedPresenceBinarySensor (state machine).

Groups:
- TestNormalMirror: NORMAL output mirrors the native presence DP.
- TestLatchTrigger: continuous presence=on >= RECOVERY_PRESENCE_ON_SEC enters RECOVERY.
- TestRecoveryOutput: RECOVERY output uses in_range only (presence ignored).
- TestRecoveryExit: a real presence=off after the cycle returns to NORMAL.
- TestEchoSuppression: presence transitions during an active cycle are ignored.
- TestHealthTick: health tick runs a silent background reset without entering RECOVERY.
- TestLatchDuringReset: latch evaluation runs even when is_resetting=True.
- TestFallbackTick: fallback tick calls _recompute for stuck-device latch detection.

How to run:
    PYTHONPATH=. pytest custom_components/sanitized_presence/tests/test_binary_sensor.py
"""

from __future__ import annotations

# pylint: disable=protected-access

import time as _time_mod
import unittest.mock as _mock
from unittest.mock import MagicMock

import asyncio

import pytest

from custom_components.sanitized_presence.binary_sensor import (
    MODE_NORMAL,
    MODE_RECOVERY,
    SanitizedPresenceBinarySensor,
)
from custom_components.sanitized_presence.const import (
    HEALTH_RESET_INTERVAL_SEC,
    RECOVERY_PRESENCE_ON_SEC,
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
        controller = MagicMock(is_resetting=False)
        # Plain MagicMock (not a coroutine): _enter_recovery schedules it via
        # hass.async_create_task, so we only need to verify it was called.
        controller.request_reset = MagicMock(return_value=True)
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

    def test_request_reset_closes_gate_synchronously(self, hass):
        """Entering RECOVERY closes the echo gate before any await.

        Validates the race fix: once request_reset is invoked it sets
        is_resetting synchronously, so a real presence=off arriving in the
        same recompute cycle cannot prematurely exit RECOVERY.
        Code: custom_components/sanitized_presence/recovery.py::RecoveryController.request_reset
        Assertion: after request_reset is called, controller.is_resetting
            is True and a subsequent _recompute with presence='off' stays
            in RECOVERY.
        Method:
        1. Arrange: a controller stub whose request_reset sets is_resetting
            True synchronously (mirroring the real implementation).
        2. Act: force RECOVERY, call request_reset, then recompute off.
        3. Assert: mode stays RECOVERY.
        """
        controller = MagicMock()
        controller.is_resetting = False

        def _req(_reason):
            controller.is_resetting = True
            return True  # not awaited in this unit test (async_create_task mocked)

        controller.request_reset = MagicMock(side_effect=_req)
        sensor = _make_sensor(hass, controller)
        sensor._mode = MODE_RECOVERY
        _states(hass, presence="off")

        controller.request_reset("latch")  # gate closes synchronously
        sensor._recompute(now=10.0)

        assert sensor._mode == MODE_RECOVERY


class TestHealthTick:
    """Health tick performs a silent background reset, never enters RECOVERY."""

    def test_health_tick_does_not_enter_recovery(self, hass):
        """Health tick in NORMAL schedules request_reset('health') and stays NORMAL.

        Validates: the decoupling fix — the periodic health reset is a
        background firmware-freshness walk; it must NOT change mode or output
        semantics.
        Code: binary_sensor.py::SanitizedPresenceBinarySensor._on_health_tick
        Assertion: after a due health tick, mode == NORMAL and
            controller.request_reset was called with 'health'.
        Method:
        1. Arrange: NORMAL; health due (_last_reset_anchor set in the past).
        2. Act: _on_health_tick(None).
        3. Assert: mode NORMAL; hass.async_create_task called; request_reset
           called with 'health'.
        """
        controller = MagicMock(is_resetting=False)
        controller.request_reset = MagicMock(return_value=True)
        sensor = _make_sensor(hass, controller)
        _states(hass, presence="off")
        # Back-date anchor so health interval is due.
        sensor._last_reset_anchor = 0.0

        sensor._on_health_tick(None)

        assert sensor._mode == MODE_NORMAL
        controller.request_reset.assert_called_once_with("health")
        hass.async_create_task.assert_called()

    def test_health_tick_retries_in_recovery_mode(self, hass):
        """Health tick in RECOVERY schedules request_reset('recovery-retry') when due.

        Validates: the periodic retry fix — if the firmware never recovered after
        the initial reset cycle (native presence still stuck on), the health tick
        must fire a recovery-retry reset so we don't sit in RECOVERY forever.
        Code: binary_sensor.py::SanitizedPresenceBinarySensor._on_health_tick
        Assertion: with mode == RECOVERY and interval due, request_reset is
            called once with 'recovery-retry'; mode stays RECOVERY.
        Method:
        1. Arrange: mode RECOVERY; _last_reset_anchor far in the past (due).
        2. Act: _on_health_tick(None).
        3. Assert: request_reset called once with 'recovery-retry'; mode RECOVERY.
        """
        controller = MagicMock(is_resetting=True)
        controller.request_reset = MagicMock(return_value=True)
        sensor = _make_sensor(hass, controller)
        sensor._mode = MODE_RECOVERY
        _states(hass, presence="on")
        sensor._last_reset_anchor = _time_mod.time() - HEALTH_RESET_INTERVAL_SEC - 1

        sensor._on_health_tick(None)

        assert sensor._mode == MODE_RECOVERY
        controller.request_reset.assert_called_once_with("recovery-retry")
        hass.async_create_task.assert_called()

    def test_health_tick_skipped_in_recovery_when_not_due(self, hass):
        """Health tick in RECOVERY is a no-op when the interval has not elapsed.

        Validates: the retry is rate-limited to HEALTH_RESET_INTERVAL_SEC cadence;
        a tick arriving before the interval elapses must not schedule a reset.
        Code: binary_sensor.py::SanitizedPresenceBinarySensor._on_health_tick
        Assertion: with mode == RECOVERY and interval NOT due, request_reset is
            not called.
        Method:
        1. Arrange: mode RECOVERY; _last_reset_anchor 100s ago (not due).
        2. Act: _on_health_tick(None).
        3. Assert: request_reset NOT called; mode unchanged RECOVERY.
        """
        controller = MagicMock(is_resetting=True)
        controller.request_reset = MagicMock()
        sensor = _make_sensor(hass, controller)
        sensor._mode = MODE_RECOVERY
        _states(hass, presence="on")
        sensor._last_reset_anchor = _time_mod.time() - 100

        sensor._on_health_tick(None)

        assert sensor._mode == MODE_RECOVERY
        controller.request_reset.assert_not_called()

    def test_health_tick_skipped_when_task_in_flight(self, hass):
        """Health tick is a no-op when a reset task is already in flight.

        Validates: no double-stacking — if a reset coroutine is still running
        (reset_task not done), we must not schedule another one regardless of
        mode or interval.
        Code: binary_sensor.py::SanitizedPresenceBinarySensor._on_health_tick
        Assertion: with a pending _reset_task, request_reset is NOT called even
            if interval is due.
        Method:
        1. Arrange: mode RECOVERY; interval due; _reset_task = pending future.
        2. Act: _on_health_tick(None).
        3. Assert: request_reset NOT called.
        """
        controller = MagicMock(is_resetting=True)
        controller.request_reset = MagicMock(return_value=True)
        sensor = _make_sensor(hass, controller)
        sensor._mode = MODE_RECOVERY
        _states(hass, presence="on")
        sensor._last_reset_anchor = _time_mod.time() - HEALTH_RESET_INTERVAL_SEC - 1

        # Simulate an in-flight task (not done)
        loop = asyncio.new_event_loop()
        pending_future = loop.create_future()
        sensor._reset_task = pending_future

        try:
            sensor._on_health_tick(None)
            controller.request_reset.assert_not_called()
        finally:
            pending_future.cancel()
            loop.close()

    def test_health_tick_uses_health_reason_in_normal(self, hass):
        """Health tick in NORMAL schedules request_reset('health'), not 'recovery-retry'.

        Regression guard: the NORMAL-mode behavior must be unchanged after the
        RECOVERY retry was added; the reason string must stay 'health'.
        Code: binary_sensor.py::SanitizedPresenceBinarySensor._on_health_tick
        Assertion: in NORMAL mode with interval due, request_reset is called
            with 'health'; mode stays NORMAL.
        Method:
        1. Arrange: mode NORMAL; _last_reset_anchor far in the past (due).
        2. Act: _on_health_tick(None).
        3. Assert: request_reset called once with 'health'; mode NORMAL.
        """
        controller = MagicMock(is_resetting=False)
        controller.request_reset = MagicMock(return_value=True)
        sensor = _make_sensor(hass, controller)
        sensor._mode = MODE_NORMAL
        _states(hass, presence="off")
        sensor._last_reset_anchor = _time_mod.time() - HEALTH_RESET_INTERVAL_SEC - 1

        sensor._on_health_tick(None)

        assert sensor._mode == MODE_NORMAL
        controller.request_reset.assert_called_once_with("health")

    def test_normal_mode_output_frozen_during_reset(self, hass):
        """Phantom presence=on during a health reset is suppressed in NORMAL.

        Validates: echo suppression for the health path — the firmware emits
        a spurious presence=on during the select-walk; in NORMAL mode the
        sanitized output must not flip on due to this phantom.
        Code: binary_sensor.py::SanitizedPresenceBinarySensor._recompute
        Assertion: with is_resetting=True in NORMAL, presence='on' does NOT
            set _attr_is_on to True.
        Method:
        1. Arrange: NORMAL; output False; controller.is_resetting=True.
        2. Act: _recompute with presence='on'.
        3. Assert: _attr_is_on remains False.
        """
        controller = MagicMock(is_resetting=True)
        sensor = _make_sensor(hass, controller)
        sensor._attr_is_on = False
        _states(hass, presence="on")

        sensor._recompute(now=0.0)

        assert sensor._attr_is_on is False
        assert sensor._mode == MODE_NORMAL

    def test_normal_mode_mirrors_presence_when_not_resetting(self, hass):
        """NORMAL mode mirrors presence faithfully when no reset is in flight.

        Regression guard: the echo-suppression freeze must not break normal
        mirror semantics when is_resetting is False.
        Code: binary_sensor.py::SanitizedPresenceBinarySensor._recompute
        Assertion: presence 'on' -> True; presence 'off' -> False; both with
            is_resetting=False.
        Method:
        1. Arrange: NORMAL; controller.is_resetting=False.
        2. Act: _recompute with presence 'on' then 'off'.
        3. Assert: outputs match presence.
        """
        controller = MagicMock(is_resetting=False)
        sensor = _make_sensor(hass, controller)

        _states(hass, presence="on")
        sensor._recompute(now=0.0)
        assert sensor._attr_is_on is True

        _states(hass, presence="off")
        sensor._recompute(now=1.0)
        assert sensor._attr_is_on is False

    def test_health_reset_does_not_trip_latch(self, hass):
        """Phantom presence=on during health reset must not accumulate latch time.

        Validates: latch tracking must be frozen while is_resetting is True
        so the maintenance walk cannot itself trigger RECOVERY entry.
        Code: binary_sensor.py::SanitizedPresenceBinarySensor._recompute
        Assertion: even after > RECOVERY_PRESENCE_ON_SEC of simulated time
            with is_resetting=True, mode stays NORMAL.
        Method:
        1. Arrange: NORMAL; controller.is_resetting=True; presence='on'.
        2. Act: multiple _recompute calls spanning > RECOVERY_PRESENCE_ON_SEC.
        3. Assert: mode NORMAL throughout.
        """
        controller = MagicMock(is_resetting=True)
        sensor = _make_sensor(hass, controller)
        _states(hass, presence="on")

        # Step through time well past the latch threshold.
        for t in range(0, RECOVERY_PRESENCE_ON_SEC + 60, 60):
            sensor._recompute(now=float(t))
            assert sensor._mode == MODE_NORMAL, f"tripped at t={t}"

    def test_latch_still_enters_recovery(self, hass):
        """Real latch (is_resetting=False) still enters RECOVERY.

        Regression guard: echo-suppression changes must not disable the
        fundamental stuck-presence detection.
        Code: binary_sensor.py::SanitizedPresenceBinarySensor._recompute
        Assertion: with is_resetting=False, presence='on' continuously >=
            RECOVERY_PRESENCE_ON_SEC causes mode RECOVERY.
        Method:
        1. Arrange: NORMAL; controller.is_resetting=False; presence='on'.
        2. Act: recompute at t=0 then t=RECOVERY_PRESENCE_ON_SEC.
        3. Assert: mode RECOVERY.
        """
        controller = MagicMock(is_resetting=False)
        controller.request_reset = MagicMock(return_value=True)
        sensor = _make_sensor(hass, controller)
        _states(hass, presence="on")

        sensor._recompute(now=0.0)
        sensor._recompute(now=float(RECOVERY_PRESENCE_ON_SEC))

        assert sensor._mode == MODE_RECOVERY


class TestLatchDuringReset:
    """Latch evaluation must run even while is_resetting=True.

    Root cause of the bug: eager task factory makes is_resetting=True
    synchronously before _recompute is called from _on_health_tick.
    The old freeze early-return skipped latch evaluation entirely.
    """

    def test_latch_fires_during_in_flight_reset(self, hass):
        """Latch enters RECOVERY even when is_resetting=True.

        Validates: the core fix for the eager-task-factory race. A stuck
        native presence that has accumulated > RECOVERY_PRESENCE_ON_SEC
        must latch even if a background health reset is in flight.
        Code: binary_sensor.py::SanitizedPresenceBinarySensor._recompute
        Assertion: mode NORMAL, is_resetting=True, _presence_on_since set
            1000s ago -> _recompute causes mode RECOVERY.
        Method:
        1. Arrange: NORMAL; is_resetting=True; _presence_on_since = now-1000.
        2. Act: _recompute(now).
        3. Assert: mode == RECOVERY.
        """
        controller = MagicMock(is_resetting=True)
        controller.request_reset = MagicMock(return_value=True)
        sensor = _make_sensor(hass, controller)
        now = 10000.0
        sensor._presence_on_since = now - 1000  # 1000s > RECOVERY_PRESENCE_ON_SEC (900s)
        _states(hass, presence="on")

        sensor._recompute(now=now)

        assert sensor._mode == MODE_RECOVERY

    def test_latch_tracking_frozen_during_reset(self, hass):
        """_presence_on_since is NOT advanced by phantom presence during reset.

        Regression guard: latch tracking (the timer start) must remain frozen
        while is_resetting=True so phantom echoes from the firmware cannot
        accumulate toward the latch threshold.
        Code: binary_sensor.py::SanitizedPresenceBinarySensor._recompute
        Assertion: NORMAL, is_resetting=True, _presence_on_since=None before
            call -> still None after _recompute with presence='on'.
        Method:
        1. Arrange: NORMAL; is_resetting=True; _presence_on_since=None.
        2. Act: _recompute with presence='on'.
        3. Assert: _presence_on_since is still None.
        """
        controller = MagicMock(is_resetting=True)
        sensor = _make_sensor(hass, controller)
        sensor._presence_on_since = None
        _states(hass, presence="on")

        sensor._recompute(now=5000.0)

        assert sensor._presence_on_since is None

    def test_output_frozen_in_normal_during_reset(self, hass):
        """_attr_is_on unchanged in NORMAL when is_resetting=True and presence='on'.

        Regression guard: output freeze semantics must survive the restructure.
        A phantom 'on' during a health reset must not change the sanitized output.
        Code: binary_sensor.py::SanitizedPresenceBinarySensor._recompute
        Assertion: NORMAL, is_resetting=True, _attr_is_on=False, presence='on'
            -> _attr_is_on remains False after _recompute.
        Method:
        1. Arrange: NORMAL; is_resetting=True; _attr_is_on=False; presence='on'.
        2. Act: _recompute.
        3. Assert: _attr_is_on is False; mode is NORMAL.
        """
        controller = MagicMock(is_resetting=True)
        sensor = _make_sensor(hass, controller)
        sensor._attr_is_on = False
        # _presence_on_since=None so latch won't fire
        sensor._presence_on_since = None
        _states(hass, presence="on")

        sensor._recompute(now=5000.0)

        assert sensor._attr_is_on is False
        assert sensor._mode == MODE_NORMAL

    def test_output_mirrors_presence_in_normal_when_not_resetting(self, hass):
        """NORMAL mirrors presence correctly when is_resetting=False.

        Regression guard: the output freeze must not affect normal operation.
        Code: binary_sensor.py::SanitizedPresenceBinarySensor._recompute
        Assertion: is_resetting=False -> presence 'on' gives True, 'off' gives False.
        Method:
        1. Arrange: NORMAL; is_resetting=False.
        2. Act: _recompute with presence 'on' then 'off'.
        3. Assert: outputs match.
        """
        controller = MagicMock(is_resetting=False)
        sensor = _make_sensor(hass, controller)

        _states(hass, presence="on")
        sensor._recompute(now=0.0)
        assert sensor._attr_is_on is True

        _states(hass, presence="off")
        sensor._recompute(now=1.0)
        assert sensor._attr_is_on is False

    def test_health_reset_does_not_trip_latch_via_phantom(self, hass):
        """Phantom presence during health reset cannot accumulate latch time.

        End-to-end regression guard for the original v2026053002 freeze fix.
        If _presence_on_since=None (no prior accumulation), phantom presence
        during a reset must leave it None so the latch never fires.
        Code: binary_sensor.py::SanitizedPresenceBinarySensor._recompute
        Assertion: NORMAL, is_resetting=True, _presence_on_since=None,
            multiple _recompute calls with presence='on' over >900s -> NORMAL.
        Method:
        1. Arrange: NORMAL; is_resetting=True; _presence_on_since=None.
        2. Act: many _recompute calls with presence='on' over >RECOVERY_PRESENCE_ON_SEC.
        3. Assert: mode stays NORMAL throughout.
        """
        controller = MagicMock(is_resetting=True)
        sensor = _make_sensor(hass, controller)
        sensor._presence_on_since = None
        _states(hass, presence="on")

        for t in range(0, RECOVERY_PRESENCE_ON_SEC + 60, 60):
            sensor._recompute(now=float(t))
            assert sensor._mode == MODE_NORMAL, f"phantom latch at t={t}"


class TestFallbackTick:
    """Fallback tick must call _recompute for 60s latch detection cadence."""

    def test_fallback_tick_calls_recompute(self, hass):
        """_on_fallback_tick calls _recompute with a 'now' argument.

        Validates: the tightened latch cadence — stuck devices with no state
        events are checked every 60s via fallback tick, not every 30min.
        Code: binary_sensor.py::SanitizedPresenceBinarySensor._on_fallback_tick
        Assertion: after _on_fallback_tick, _recompute was called once with
            a float 'now' keyword argument. maybe_recover_parked also scheduled.
        Method:
        1. Arrange: patch sensor._recompute with a MagicMock.
        2. Act: call _on_fallback_tick(None).
        3. Assert: _recompute called once; hass.async_create_task called.
        """
        controller = MagicMock(is_resetting=False)
        sensor = _make_sensor(hass, controller)
        sensor._recompute = MagicMock()
        _states(hass, presence="off")

        sensor._on_fallback_tick(None)

        sensor._recompute.assert_called_once()
        call_kwargs = sensor._recompute.call_args
        # Must be called with a float 'now' keyword argument
        assert "now" in call_kwargs.kwargs
        assert isinstance(call_kwargs.kwargs["now"], float)
        hass.async_create_task.assert_called()

    def test_latch_via_fallback_tick_detects_stuck_native(self, hass):
        """Stuck native presence latches via fallback tick without state events.

        End-to-end scenario: a device stuck reporting presence='on' generates
        no state change events. Only fallback tick drives _recompute.
        Code: binary_sensor.py::SanitizedPresenceBinarySensor._on_fallback_tick
        Assertion: after _recompute(t0) arms the timer, calling _on_fallback_tick
            at t0+1000 (>900s) causes mode RECOVERY.
        Method:
        1. Arrange: NORMAL; is_resetting=False; presence='on'.
        2. Act: _recompute(t0) to arm _presence_on_since, then
            _on_fallback_tick at t0+1000.
        3. Assert: mode == RECOVERY.
        """
        controller = MagicMock(is_resetting=False)
        controller.request_reset = MagicMock(return_value=True)
        sensor = _make_sensor(hass, controller)
        _states(hass, presence="on")
        t0 = 1000.0

        sensor._recompute(now=t0)  # arms _presence_on_since = t0

        # Simulate 1000s later via fallback tick (no state events in between).
        # We need time.time() to return t0+1000 when _on_fallback_tick calls it.
        with _mock.patch.object(_time_mod, "time", return_value=t0 + 1000):
            sensor._on_fallback_tick(None)

        assert sensor._mode == MODE_RECOVERY
