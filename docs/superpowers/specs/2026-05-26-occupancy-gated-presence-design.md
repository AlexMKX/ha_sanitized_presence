# Occupancy-Gated Sanitized Presence — Design

## Problem

Today `SanitizedPresenceBinarySensor` derives presence purely from
`target_distance` being inside the configured range. The native device
`occupancy` (presence DP) is ignored. This causes false positives when the
radar reports a stale or noisy `target_distance` value while the device
itself reports no occupant.

## Goal

Sanitized presence must **confirm** the device's native occupancy, not
replace it:

```
sanitized_presence = occupancy_on AND in_range(target_distance)
```

The pulse/deadline timing model (sliding window driven by
`departure_delay`) is unchanged.

## Behavior

### Evaluation rule

`_evaluate(reason)` pulses `(timeout = clamped delay)` only when **both**:

1. The device occupancy entity's state is `"on"`.
2. `in_range(target, shield, detect, SHIELD_FLOOR_M)` is true.

Any other combination — including `occupancy in {off, unknown, unavailable}`
or `target` out of range — results in **no pulse**. We do not reset the
sensor manually; we let the existing deadline expire naturally (consistent
with how out-of-range is already handled).

### Triggers

`_evaluate` runs on:

- target_distance state change (existing) — `reason="target_change"`
- occupancy state change (new) — `reason="occupancy_change"`
- periodic tick (existing) — `reason="tick"`
- startup (existing) — `reason="startup"`

Subscribing to occupancy state changes guarantees that an occupancy
off→on transition pulses immediately, instead of waiting up to
`TICK_CEILING_S` for the next tick.

## Components

### discovery.py

Per MTG075/MTG275 device, locate the occupancy binary_sensor on the same
device:

1. Prefer `binary_sensor` entity whose `device_class == "occupancy"`.
2. Fallback: `binary_sensor` whose entity_id or original_name contains
   `presence`.
3. If none found → log warning and skip creating the sanitized
   binary_sensor for this device (same policy as missing
   `target_distance` / `detection_range` today).

Pass the resolved `occupancy_eid` into `SanitizedPresenceBinarySensor.__init__`.

### binary_sensor.py

Additions:

- New constructor arg `occupancy_eid: str`, stored as `self._occupancy_eid`.
- In `async_added_to_hass`, the existing
  `async_track_state_change_event` call subscribes to a list:
  `[self._target_distance_eid, self._occupancy_eid]`.
- The state-change handler dispatches `reason` by `event.data["entity_id"]`:
  - `target_distance_eid` → `"target_change"`
  - `occupancy_eid` → `"occupancy_change"`
  - Ignored states (`unknown`, `unavailable`) handling stays as-is for
    target; for occupancy, no early return — `_evaluate` itself treats
    those as off via the gate below.
- In `_evaluate`, before the range check:
  ```
  occ_state = hass.states.get(self._occupancy_eid)
  occupancy_on = occ_state is not None and occ_state.state == "on"
  if not occupancy_on:
      _LOGGER.debug("_evaluate(%s): occupancy_off, skip", reason)
      self._occupancy_state = occ_state.state if occ_state else None
      return
  self._occupancy_state = "on"
  ```
- `extra_state_attributes` gains:
  - `occupancy_eid`
  - `occupancy_state` (last observed: `on` / `off` / `unknown` / `unavailable` / `None`)

### const.py

No changes. Occupancy gating has no numeric parameters.

## Edge cases

- **Occupancy entity becomes unavailable mid-life:** treated as off, no
  new pulses; any active deadline expires normally, sensor turns off at
  the deadline.
- **Occupancy missing at discovery time:** device skipped with warning.
  (No silent fallback to "occupancy always true" — that would defeat the
  purpose of this change.)
- **Occupancy entity ID resolves to a different domain (e.g., sensor)
  due to user customization:** discovery accepts only `binary_sensor`
  domain; otherwise skip with warning.

## Tests

New tests in `custom_components/sanitized_presence/tests/`:

1. `test_evaluate_occupancy_off_in_range_no_pulse` — occupancy=off,
   target in range → `pulse` not called.
2. `test_evaluate_occupancy_on_in_range_pulses` — both true → `pulse`
   called with clamped timeout.
3. `test_evaluate_occupancy_unknown_no_pulse` — occupancy=unknown,
   target in range → no pulse.
4. `test_occupancy_off_to_on_triggers_evaluate` — state change subscription
   fires `_evaluate` and pulses when target already stable in range.
5. `test_discovery_finds_occupancy_by_device_class` — manager wires
   `occupancy_eid` correctly.
6. `test_discovery_skips_device_without_occupancy` — no occupancy entity
   → no sanitized sensor created, warning logged.

Existing tests for in_range / pulse / deadline behavior must remain
green; the gate is additive and inserted before the existing range
check.

## Non-goals

- No options-flow override for occupancy entity (discovery-only,
  per project preference).
- No change to `DeadlineSensorEntity`, `AutoResetBinarySensor`,
  `helpers.py`.
- No change to delay/tick clamping constants.
