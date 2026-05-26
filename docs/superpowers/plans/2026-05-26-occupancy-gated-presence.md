# Occupancy-Gated Sanitized Presence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `SanitizedPresenceBinarySensor` confirm the device's native occupancy DP — pulse only when `occupancy = on` AND target in range.

**Architecture:** Add a fifth required Z2M entity (`occupancy`) to discovery, pass its entity_id into the binary sensor, subscribe to its state changes alongside `target_distance`, and add a gate at the top of `_evaluate` that no-ops when occupancy is not `on`. The pulse/deadline model is unchanged — out-of-gate evaluations simply skip the pulse and let the existing deadline expire naturally.

**Tech Stack:** Home Assistant custom_component (Python), pytest, Zigbee2MQTT entity unique_id convention (`_<suffix>_zigbee2mqtt`).

**Discovery note:** The spec proposed `device_class == "occupancy"` with a name fallback. To match the existing pattern in `discovery.py` (which uses unique_id suffix matching for the other four DPs), we use `SUFFIX_OCCUPANCY = "occupancy"` — Z2M's standard naming for this DP. This keeps a single mechanism instead of introducing two.

---

### Task 1: Add SUFFIX_OCCUPANCY constant

**Files:**
- Modify: `custom_components/sanitized_presence/const.py`

- [ ] **Step 1: Add the constant and export it**

Edit `custom_components/sanitized_presence/const.py`. Add after `SUFFIX_DEPARTURE_DELAY`:

```python
SUFFIX_OCCUPANCY = "occupancy"
```

And add `"SUFFIX_OCCUPANCY",` to the `__all__` list, immediately after `"SUFFIX_DEPARTURE_DELAY",`.

- [ ] **Step 2: Sanity import**

Run:
```bash
python -c "from custom_components.sanitized_presence.const import SUFFIX_OCCUPANCY; print(SUFFIX_OCCUPANCY)"
```
Expected output: `occupancy`

- [ ] **Step 3: Commit**

```bash
git add custom_components/sanitized_presence/const.py
git commit -m "feat(const): add SUFFIX_OCCUPANCY for occupancy DP discovery"
```

---

### Task 2: Discovery — require and resolve occupancy entity

**Files:**
- Modify: `custom_components/sanitized_presence/discovery.py`
- Test: `custom_components/sanitized_presence/tests/test_discovery.py`

- [ ] **Step 1: Write a failing test — occupancy required**

Append to `custom_components/sanitized_presence/tests/test_discovery.py` inside `class TestSanitizedPresenceManager`:

```python
    def test_device_missing_occupancy_entity_is_skipped(self, manager):
        """A device without an occupancy entity is excluded from discovery.

        Validates: occupancy is now a required DP — without it, the
        sanitized sensor cannot apply its gating rule, so creating it
        would be misleading. Discovery must skip such devices with a
        warning, matching the existing policy for other required DPs.
        Code: custom_components/sanitized_presence/discovery.py::SanitizedPresenceManager._resolve_entities
        Assertion: _sensors is empty after discovery when occupancy
            entity is missing.
        Method:
        1. Arrange: device with the four legacy entities but no occupancy.
        2. Act: patch devices_by_model; call _find_target_devices.
        3. Assert: _sensors == {} (no pair registered).
        """
        # Note: _full_entity_map() must include "occupancy" by now;
        # this scenario deletes it explicitly.
        partial = _full_entity_map()
        del partial["occupancy"]
        dev = _make_device("d1", "MTG075-ZB-RL", partial)
        with patch(
            "custom_components.sanitized_presence.discovery.devices_by_model",
            return_value=[dev],
        ):
            manager._find_target_devices()

        assert manager._sensors == {}
```

Also update the existing `_full_entity_map` helper at the module top to include occupancy:

```python
def _full_entity_map():
    return {
        "target_distance": "sensor.r_target_distance",
        "detection_range": "number.r_detection_range",
        "shield_range": "number.r_shield_range",
        "departure_delay": "number.r_departure_delay",
        "occupancy": "binary_sensor.r_occupancy",
    }
```

- [ ] **Step 2: Run failing test**

```bash
pytest custom_components/sanitized_presence/tests/test_discovery.py -v
```
Expected: `test_device_missing_occupancy_entity_is_skipped` FAILS (occupancy not yet required) AND `test_repeated_discovery_does_not_duplicate_entities` may also break because the binary sensor constructor doesn't yet accept `occupancy_eid` — that's OK, both will pass after Tasks 2+3. For this checkpoint, confirm the new test fails for the right reason (no warning / no skip).

- [ ] **Step 3: Add occupancy to required suffixes and pass to constructor**

Edit `custom_components/sanitized_presence/discovery.py`:

Update imports:
```python
from .const import (
    CONF_POLL_INTERVAL,
    DEFAULT_POLL_S,
    SUFFIX_DEPARTURE_DELAY,
    SUFFIX_DETECTION_RANGE,
    SUFFIX_OCCUPANCY,
    SUFFIX_SHIELD_RANGE,
    SUFFIX_TARGET_DISTANCE,
    TARGET_MODELS,
)
```

Update `_REQUIRED_SUFFIXES`:
```python
_REQUIRED_SUFFIXES = (
    SUFFIX_TARGET_DISTANCE,
    SUFFIX_DETECTION_RANGE,
    SUFFIX_SHIELD_RANGE,
    SUFFIX_DEPARTURE_DELAY,
    SUFFIX_OCCUPANCY,
)
```

Update the `SanitizedPresenceBinarySensor(...)` call in `_discover_and_add_sensors` to pass occupancy:
```python
            binary_sensor = SanitizedPresenceBinarySensor(
                hass=self.hass,
                entry=self.entry,
                device_id=device.id,
                device_name=device.name,
                device_identifiers=device.identifiers,
                target_distance_eid=eids[SUFFIX_TARGET_DISTANCE],
                detection_range_eid=eids[SUFFIX_DETECTION_RANGE],
                shield_range_eid=eids[SUFFIX_SHIELD_RANGE],
                departure_delay_eid=eids[SUFFIX_DEPARTURE_DELAY],
                occupancy_eid=eids[SUFFIX_OCCUPANCY],
            )
```

- [ ] **Step 4: Run discovery tests — they will still fail on the constructor**

```bash
pytest custom_components/sanitized_presence/tests/test_discovery.py -v
```
Expected: `test_device_missing_occupancy_entity_is_skipped` PASSES now; `test_repeated_discovery_does_not_duplicate_entities` FAILS with `TypeError: __init__ got unexpected keyword argument 'occupancy_eid'`. That is expected — Task 3 fixes it.

- [ ] **Step 5: Commit**

```bash
git add custom_components/sanitized_presence/discovery.py custom_components/sanitized_presence/tests/test_discovery.py
git commit -m "feat(discovery): require occupancy entity and pass to binary sensor"
```

---

### Task 3: Binary sensor — accept occupancy_eid, subscribe, gate in `_evaluate`

**Files:**
- Modify: `custom_components/sanitized_presence/binary_sensor.py`
- Test: `custom_components/sanitized_presence/tests/test_binary_sensor.py`

- [ ] **Step 1: Update test helpers to pass occupancy_eid**

Edit `custom_components/sanitized_presence/tests/test_binary_sensor.py`.

Update `_make_sensor`:
```python
def _make_sensor(hass, *, target_eid="sensor.radar_target_distance"):
    entry = MagicMock()
    entry.entry_id = "e1"
    return SanitizedPresenceBinarySensor(
        hass=hass,
        entry=entry,
        device_id="dev1",
        device_name="Radar 1",
        device_identifiers={("zigbee2mqtt", "0xABCD")},
        target_distance_eid=target_eid,
        detection_range_eid="number.radar_detection_range",
        shield_range_eid="number.radar_shield_range",
        departure_delay_eid="number.radar_departure_delay",
        occupancy_eid="binary_sensor.radar_occupancy",
    )
```

Update `_setup_hass_states` to also serve occupancy. New signature with default `occupancy="on"`:

```python
def _setup_hass_states(hass, target, detect, shield, delay, occupancy="on"):
    """Wire hass.states.get(...) to return mocked states for the five eids."""

    def _state_or_unavailable(value):
        return _make_state(str(value)) if value is not None else _make_state("unavailable")

    def _occ_state(value):
        # occupancy is a literal string state, not numeric; None => unavailable
        if value is None:
            return _make_state("unavailable")
        return _make_state(value)

    mapping = {
        "sensor.radar_target_distance": _state_or_unavailable(target),
        "number.radar_detection_range": _state_or_unavailable(detect),
        "number.radar_shield_range": _state_or_unavailable(shield),
        "number.radar_departure_delay": _state_or_unavailable(delay),
        "binary_sensor.radar_occupancy": _occ_state(occupancy),
    }
    hass.states.get.side_effect = mapping.get
```

- [ ] **Step 2: Add failing tests for the occupancy gate**

Append to `class TestEvaluatePulseDecision` in the same file:

```python
    def test_occupancy_off_in_range_does_not_pulse(self, hass):
        """occupancy=off with target in range: gate blocks the pulse.

        Validates: the new semantics — sanitized_presence confirms the
        device's native occupancy. Even with a target inside the window,
        an `off` occupancy DP must suppress the pulse so the existing
        deadline can expire naturally (matches the documented out-of-
        range behavior).
        Code: custom_components/sanitized_presence/binary_sensor.py::SanitizedPresenceBinarySensor._evaluate
        Assertion: pulse is not called when occupancy=off.
        Method:
        1. Arrange: target in range, occupancy="off".
        2. Act: patch pulse, call _evaluate("test").
        3. Assert: pulse not called.
        """
        sensor = _make_sensor(hass)
        _setup_hass_states(hass, target=1.5, detect=4.5, shield=0.0, delay=30, occupancy="off")

        with patch.object(sensor, "pulse") as mock_pulse:
            sensor._evaluate("test")

        mock_pulse.assert_not_called()

    @pytest.mark.parametrize("occ_value", ["unknown", "unavailable", None])
    def test_occupancy_unknown_or_unavailable_does_not_pulse(self, hass, occ_value):
        """Non-`on` occupancy states (unknown/unavailable/missing) gate off.

        Validates: the gate is permissive only for the explicit `on`
        state, so a flaky/missing occupancy entity cannot accidentally
        let stale target_distance readings drive sanitized_presence.
        Code: custom_components/sanitized_presence/binary_sensor.py::SanitizedPresenceBinarySensor._evaluate
        Assertion: pulse is not called for any non-"on" occupancy value.
        Method:
        1. Arrange: target in range, occupancy in {unknown, unavailable, None}.
        2. Act: patch pulse, call _evaluate.
        3. Assert: pulse not called.
        """
        sensor = _make_sensor(hass)
        _setup_hass_states(
            hass, target=1.5, detect=4.5, shield=0.0, delay=30, occupancy=occ_value
        )

        with patch.object(sensor, "pulse") as mock_pulse:
            sensor._evaluate("test")

        mock_pulse.assert_not_called()

    def test_occupancy_on_in_range_pulses(self, hass):
        """occupancy=on with target in range still pulses (regression guard).

        Validates: the additive gate did not break the happy path; the
        existing in-range pulse behavior is preserved when occupancy is
        explicitly on.
        Code: custom_components/sanitized_presence/binary_sensor.py::SanitizedPresenceBinarySensor._evaluate
        Assertion: pulse called once with timeout=30.0.
        Method:
        1. Arrange: target in range, occupancy="on", delay=30.
        2. Act: patch pulse, call _evaluate.
        3. Assert: pulse called with timeout=30.0.
        """
        sensor = _make_sensor(hass)
        _setup_hass_states(hass, target=1.5, detect=4.5, shield=0.0, delay=30, occupancy="on")

        with patch.object(sensor, "pulse") as mock_pulse:
            sensor._evaluate("test")

        mock_pulse.assert_called_once_with(timeout=30.0)
```

Append to `class TestTargetEventHandling`:

```python
    def test_occupancy_state_change_triggers_evaluate(self, hass):
        """An occupancy state_changed event invokes _evaluate("occupancy_change").

        Validates: the binary sensor subscribes to occupancy as well as
        target_distance, so an off->on occupancy transition pulses
        immediately instead of waiting up to TICK_CEILING_S for the
        next periodic tick.
        Code: custom_components/sanitized_presence/binary_sensor.py::SanitizedPresenceBinarySensor._handle_source_event
        Assertion: _evaluate called once with reason "occupancy_change".
        Method:
        1. Arrange: build an event with entity_id pointing to occupancy
            and new_state="on".
        2. Act: patch _evaluate, call _handle_source_event(event).
        3. Assert: _evaluate called once with "occupancy_change".
        """
        sensor = _make_sensor(hass)
        event = MagicMock()
        event.data = {
            "entity_id": "binary_sensor.radar_occupancy",
            "new_state": _make_state("on"),
            "old_state": _make_state("off"),
        }
        with patch.object(sensor, "_evaluate") as mock_eval:
            sensor._handle_source_event(event)

        mock_eval.assert_called_once_with("occupancy_change")
```

Also update the existing `test_numeric_state_change_triggers_evaluate` and
`test_sentinel_state_changes_are_ignored` tests to use the new handler
name `_handle_source_event` AND to set `entity_id` in `event.data`:

```python
        event.data = {
            "entity_id": "sensor.radar_target_distance",
            "new_state": _make_state("2.0"),
            "old_state": _make_state("1.9"),
        }
        ...
        sensor._handle_source_event(event)
```

And in the sentinel test:
```python
        event.data = {
            "entity_id": "sensor.radar_target_distance",
            "new_state": _make_state(sentinel),
            "old_state": _make_state("1.5"),
        }
        ...
        sensor._handle_source_event(event)
```

Add a new attribute test inside `class TestEvaluateAttributes`:

```python
    def test_attributes_expose_occupancy_state(self, hass):
        """extra_state_attributes expose the last observed occupancy state.

        Validates: the diagnostic surface includes occupancy so a user
        debugging a "never on" sanitized sensor can see whether the
        gate, not the range check, is suppressing the pulse.
        Code: custom_components/sanitized_presence/binary_sensor.py::SanitizedPresenceBinarySensor.extra_state_attributes
        Assertion: attrs["occupancy_eid"] is the configured eid;
            attrs["occupancy_state"] equals the last observed string.
        Method:
        1. Arrange: occupancy="off", target in range; mock pulse.
        2. Act: call _evaluate("tick").
        3. Assert: occupancy_eid and occupancy_state present and correct.
        """
        sensor = _make_sensor(hass)
        _setup_hass_states(hass, target=1.5, detect=4.5, shield=0.0, delay=30, occupancy="off")

        with patch.object(sensor, "pulse"):
            sensor._evaluate("tick")

        attrs = sensor.extra_state_attributes
        assert attrs["occupancy_eid"] == "binary_sensor.radar_occupancy"
        assert attrs["occupancy_state"] == "off"
```

- [ ] **Step 3: Run new tests — must fail**

```bash
pytest custom_components/sanitized_presence/tests/test_binary_sensor.py -v
```
Expected: the new tests FAIL (no `occupancy_eid` ctor arg, no `_handle_source_event`, no `occupancy_state` attr). Existing tests will also fail because of the helper changes — that is expected and the implementation step fixes them all together.

- [ ] **Step 4: Implement occupancy support in `binary_sensor.py`**

Edit `custom_components/sanitized_presence/binary_sensor.py`.

Update the constructor signature and body — add `occupancy_eid` parameter and store it:

```python
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
        departure_delay_eid: str,
        occupancy_eid: str,
    ) -> None:
        super().__init__(hass, reset_timeout=DEFAULT_DELAY_S)
        self._entry = entry
        self._device_id = device_id
        self._device_identifiers = device_identifiers
        self._target_distance_eid = target_distance_eid
        self._detection_range_eid = detection_range_eid
        self._shield_range_eid = shield_range_eid
        self._departure_delay_eid = departure_delay_eid
        self._occupancy_eid = occupancy_eid
        self._attr_name = f"{device_name} Sanitized Presence"
        self._attr_unique_id = f"{device_id}_sanitized_presence"
        self._unsub_state: Callable[[], None] | None = None
        self._cancel_tick: Callable[[], None] | None = None
        self._deadline_sensor = None
        # Attributes updated on every _evaluate call
        self._last_eval_reason: str = "startup"
        self._effective_min: float | None = None
        self._effective_max: float | None = None
        self._effective_timeout: float | None = None
        self._occupancy_state: str | None = None
```

Replace `async_added_to_hass`'s state subscription to track both source entities:

```python
        self._unsub_state = async_track_state_change_event(
            self.hass,
            [self._target_distance_eid, self._occupancy_eid],
            self._handle_source_event,
        )
```

Rename `_handle_target_event` to `_handle_source_event` and dispatch by entity_id:

```python
    @callback
    def _handle_source_event(self, event) -> None:
        entity_id = event.data.get("entity_id")
        new_state = event.data.get("new_state")
        if new_state is None or new_state.state in _IGNORED_STATES:
            # target_distance ignored states are skipped; occupancy ignored
            # states are handled inside _evaluate's gate, so for occupancy
            # we still want to re-evaluate on transitions into them.
            if entity_id == self._occupancy_eid:
                self._evaluate("occupancy_change")
            return
        if entity_id == self._occupancy_eid:
            self._evaluate("occupancy_change")
        else:
            self._evaluate("target_change")
```

Add the gate at the top of `_evaluate` (right after `self._last_eval_reason = reason`, before the `if detect is None or target is None:` check):

```python
        occ_state = self.hass.states.get(self._occupancy_eid)
        occ_value = occ_state.state if occ_state is not None else None
        self._occupancy_state = occ_value
        if occ_value != "on":
            _LOGGER.debug(
                "_evaluate(%s): occupancy_off (%s), skip",
                reason,
                occ_value,
            )
            return
```

Extend `extra_state_attributes`:

```python
    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "device_id": self._device_id,
            "target_distance_eid": self._target_distance_eid,
            "detection_range_eid": self._detection_range_eid,
            "shield_range_eid": self._shield_range_eid,
            "departure_delay_eid": self._departure_delay_eid,
            "occupancy_eid": self._occupancy_eid,
            "occupancy_state": self._occupancy_state,
            "effective_min": self._effective_min,
            "effective_max": self._effective_max,
            "effective_timeout": self._effective_timeout,
            "last_eval_reason": self._last_eval_reason,
        }
```

- [ ] **Step 5: Run binary sensor tests — must pass**

```bash
pytest custom_components/sanitized_presence/tests/test_binary_sensor.py -v
```
Expected: all tests PASS.

- [ ] **Step 6: Run discovery tests — they should pass now too**

```bash
pytest custom_components/sanitized_presence/tests/test_discovery.py -v
```
Expected: all tests PASS (the constructor now accepts `occupancy_eid`).

- [ ] **Step 7: Commit**

```bash
git add custom_components/sanitized_presence/binary_sensor.py custom_components/sanitized_presence/tests/test_binary_sensor.py
git commit -m "feat(binary_sensor): gate sanitized presence on native occupancy DP"
```

---

### Task 4: Integration smoke test update

**Files:**
- Modify: `custom_components/sanitized_presence/tests/test_integration_e2e.py`

- [ ] **Step 1: Read the existing e2e test to find fixture points**

```bash
pytest custom_components/sanitized_presence/tests/test_integration_e2e.py -v --collect-only
```
Expected: lists existing e2e test ids.

Open `custom_components/sanitized_presence/tests/test_integration_e2e.py` and identify any place where a fake device is built with the four DPs. The smoke test needs a fifth: an occupancy `binary_sensor` for the same device with unique_id suffix `_occupancy_zigbee2mqtt`, set to state `on` by default (so the existing happy-path expectations still hold after the gate is added).

- [ ] **Step 2: Add occupancy entity to the e2e device fixture**

Wherever the test creates the four entities (target_distance, detection_range, shield_range, departure_delay) for the MTG device, add a fifth:

```python
# Add alongside the other entity_registry inserts for the same device.
ent_reg.async_get_or_create(
    domain="binary_sensor",
    platform="zigbee2mqtt",
    unique_id="0xABCD_occupancy_zigbee2mqtt",
    suggested_object_id="radar_occupancy",
    device_id=device.id,
)
hass.states.async_set("binary_sensor.radar_occupancy", "on")
```

Use the actual variable names from the existing e2e file (the snippet above is the structure; align with the surrounding code).

- [ ] **Step 3: Run the e2e test**

```bash
pytest custom_components/sanitized_presence/tests/test_integration_e2e.py -v
```
Expected: PASS.

- [ ] **Step 4: Run the full test suite**

```bash
pytest custom_components/sanitized_presence/tests/ -v
```
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add custom_components/sanitized_presence/tests/test_integration_e2e.py
git commit -m "test(e2e): provide occupancy entity in radar smoke fixture"
```

---

### Task 5: Full verification

- [ ] **Step 1: Run the entire test suite from repo root**

```bash
pytest -v
```
Expected: all tests PASS, no warnings about deprecated handler names or missing attributes.

- [ ] **Step 2: Lint check (project uses pylint per recent commits)**

```bash
pylint custom_components/sanitized_presence
```
Expected: no new warnings introduced by this change. If there are pre-existing ones, do not address them in this plan.

- [ ] **Step 3: Manual review checklist**

Verify by reading the diff (`git diff main`):
- `_handle_target_event` is fully replaced by `_handle_source_event` (no dead method left behind)
- `_REQUIRED_SUFFIXES` includes `SUFFIX_OCCUPANCY`
- No references to `device_class == "occupancy"` or "presence" name fallback (we chose suffix-only)
- `extra_state_attributes` exposes both `occupancy_eid` and `occupancy_state`

- [ ] **Step 4: Final commit if any cleanup was needed**

Only if step 3 found leftovers:
```bash
git add -A
git commit -m "chore: cleanup after occupancy gate"
```

---

## Notes for the implementer

- **Why suffix instead of device_class:** the existing discovery uses unique_id suffix matching (`_<suffix>_zigbee2mqtt`) for all four legacy DPs. Z2M consistently exports the occupancy DP with suffix `occupancy`, so reusing the same mechanism is simpler than introducing device_class lookups. If a future user reports a Z2M version that diverges, add a fallback then — not preemptively (YAGNI).
- **Why no manual reset on occupancy=off:** the spec deliberately chose the "let deadline expire naturally" behavior (variant a). Do not add a `pulse(timeout=0)` or explicit `_attr_is_on=False` write — that would change the semantics agreed in brainstorming.
- **Why rename the handler:** `_handle_target_event` is now misleading; it dispatches by entity_id. Renaming to `_handle_source_event` keeps the codebase honest. Update test references accordingly (already in the test diff above).
