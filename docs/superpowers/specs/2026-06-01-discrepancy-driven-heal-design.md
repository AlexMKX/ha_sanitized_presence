# Design: discrepancy-driven heal for stuck MTG075/MTG275 radars

Date: 2026-06-01
Status: approved (pending spec review)
Supersedes: the latch + periodic-health + distance-gating model from
`2026-05-29-recovery-state-machine-design.md`.

## Problem

The previous model had two reset triggers and a distance-gated output mode.
Live-fleet diagnosis (2026-06-01) on `f2_kabinet_presence_2` produced hard
evidence that invalidates that model:

1. **The blind periodic health reset is harmful.** Toggling the radar's
   `sensor` select `off -> on` every 30 minutes — even on a healthy,
   occupied radar — makes the firmware re-initialise its detection:
   `presence` drops to `false` for ~1 s and `target_distance` drops to `0`.

2. **The toggle damages distance re-lock ~40% of the time.** A controlled
   sweep (gap 1 s / 5 s / 10 s, occupied room, 3-5 trials each) showed
   `target_distance` failing to re-lock for 15-30 s in roughly one third of
   toggles, **independent of the off->on gap**. `presence` (the binary DP)
   recovered within ~1 s in every trial; only `target_distance` was fragile.
   Conclusion: the gap/timing is irrelevant; the off->on toggle itself is
   the hazard.

3. **distance-gating in RECOVERY is self-defeating.** Output gated on
   `in_range(distance)` is driven to `off` precisely because our own reset
   zeroed the distance — a false "clear" on an occupied room.

4. **Z2M `optimistic` was enabled for the select.** With optimistic on, Z2M
   published the new `sensor` state immediately on `set`, before the device
   confirmed. State-confirmation timings measured before that was disabled
   were therefore meaningless. With optimistic **off**, the HA select state
   reflects the real device confirmation (round-trip ~0.9 s), which makes a
   confirmation-gated toggle feasible.

Working hypothesis for the stuck state: a cluster write (the `sensor`
mode change, or a presence/distance report) intermittently does not reach
the radar, leaving `presence == on` while `distance` contradicts it.

## Goal

Only ever toggle a radar that is demonstrably stuck, judge the result by
observation, bound the effort, and never let the messy toggle aftermath
leak into the sanitized output.

## Output model

`presence` (binary occupancy DP) is the only trusted output signal.
`distance` is used **only** for stuck detection and for heal stop
conditions — never as the output value directly. This removes the
distance-gating output mode entirely.

- **NORMAL**: output mirrors native `presence` verbatim.
- **HEALING**: output is frozen at the value held when HEALING was entered.
- **BACKOFF**: output mirrors native `presence` (same as NORMAL); only the
  heal re-entry is suppressed until the backoff timer expires.

## State machine

### Discrepancy tracking (in NORMAL / BACKOFF)

A monotonic timestamp `discrepancy_since`:

- **Set** (if currently unset) when `presence == on AND NOT in_range(d)`,
  where `in_range(d)` is `shield < d < detection` with the existing
  `SHIELD_FLOOR_M` floor.
- **Cleared** (set to `None`) when `presence == off` OR `in_range(d)` holds.

Rationale: a healthy occupied radar reports `presence=on` with `distance`
in range; a still person may briefly show `distance=0`, but not for
`DISCREPANCY_TRIGGER_SEC` continuously. A stuck radar holds
`presence=on` with bad distance indefinitely.

### NORMAL -> HEALING

Enter HEALING when:

```
discrepancy_since is not None
AND (now - discrepancy_since) >= DISCREPANCY_TRIGGER_SEC
AND now >= backoff_until
```

On entry: snapshot the current output value and freeze it.

### HEALING iteration

Each iteration is **confirmed toggle** then **observe**:

1. **Confirmed toggle** (relies on Z2M optimistic=off; see Prerequisites):
   - call `select_option('off')`; wait until the select entity reports
     `off`, up to `HEAL_CMD_TIMEOUT_SEC`;
   - call `select_option('on')`; wait until the select reports `on`, up to
     `HEAL_CMD_TIMEOUT_SEC`.
   - If either confirm times out, the command did not reach the radar
     (the working hypothesis). The iteration still counts; proceed to
     observe (the radar may yet settle) — do not abort the heal.
2. **Observe** for `HEAL_OBSERVE_SEC`, evaluating on each presence/distance
   state event plus a periodic tick:
   - **Stop A (healthy occupied)**: `in_range(distance) AND presence == on`
     sustained for `STOP_A_CONFIRM_SEC` (guards against a single-sample
     fluctuation). Result `occupied`: set output `on`, return to NORMAL,
     clear `discrepancy_since`.
   - **Stop B (healthy empty)**: the **entire** observe window had
     `presence == off`. Result `empty`: set output `off`, return to NORMAL,
     clear `discrepancy_since`.
   - **Neither**: start the next iteration.

### Iteration cap -> BACKOFF

After `HEAL_MAX_ITERATIONS` consecutive iterations without Stop A or B:

- set `backoff_until = now + HEAL_BACKOFF_SEC`;
- result `gave_up`;
- return to NORMAL behaviour for output (unfreeze; mirror presence).
  `discrepancy_since` continues to run, but heal cannot re-enter until
  `now >= backoff_until`. If the discrepancy persists past the backoff,
  HEALING re-enters automatically.

### HEALING -> exit summary

| Exit       | Output set to | discrepancy_since | Next state |
|------------|---------------|-------------------|------------|
| Stop A     | on            | cleared           | NORMAL     |
| Stop B     | off           | cleared           | NORMAL     |
| Iter cap   | mirror (on)   | kept running      | BACKOFF    |

## Park-fallback (safety net, retained)

Every `PARK_FALLBACK_INTERVAL_SEC`, if NOT currently HEALING and the select
is parked at any non-`on` value (`off`/`unoccupied`/etc.), restore it to
`on`. This recovers a select left parked by an interrupted heal (e.g. HA
restart mid-iteration). Guarded so it never fights an in-flight heal toggle.

## Components

- **`SanitizedPresenceBinarySensor`** (entity, thin): owns discrepancy
  tracking, the NORMAL/HEALING/BACKOFF state, output, and freeze. Delegates
  the confirmed toggle + observe loop to the controller.
- **`HealController`** (renamed/rewritten from `RecoveryController`): owns
  the confirmed toggle (`off`->confirm->`on`->confirm with timeout), the
  observe loop with Stop A/B evaluation, the iteration cap + backoff, and
  the park-fallback. Exposes `is_healing` (echo/guard gate) and
  `diagnostics()`.
- **`StatusSensorEntity`**: diagnostics surface (see below). `unique_id`
  suffix unchanged for registry continuity.

## Constants

```python
DISCREPANCY_TRIGGER_SEC    = 900    # presence=on AND not in_range, sustained
HEAL_OBSERVE_SEC           = 60     # observation window per iteration
HEAL_MAX_ITERATIONS        = 5      # consecutive iterations before backoff
HEAL_BACKOFF_SEC           = 1800   # pause after giving up
HEAL_CMD_TIMEOUT_SEC       = 10     # wait for confirmed off / on
STOP_A_CONFIRM_SEC         = 5      # sustained in_range+presence_on for Stop A
PARK_FALLBACK_INTERVAL_SEC = 60     # park-fallback poll cadence
SENSOR_RESET_SEQUENCE      = ("off", "on")  # no intermediate 'unoccupied'
SHIELD_FLOOR_M             = 0.1    # retained
```

Removed (anti-bloat): `RECOVERY_PRESENCE_ON_SEC`, `HEALTH_RESET_INTERVAL_SEC`,
`SENSOR_PHASE_DELAY_SEC`, `RESET_COOLDOWN_SEC`, `RESET_RATE_WINDOW_SEC`,
`RESET_RATE_LIMIT`, `RESET_RATE_BLOCK_SEC`. The distance-gating output path
(`_compute_output` via `in_range`) and the periodic health tick are deleted.

## Prerequisites (deployment)

- **Z2M `optimistic` must be disabled** for the radar `sensor` select so the
  HA select state reflects real device confirmation. The confirmed-toggle
  detection of "command did not reach the radar" depends on this.
- **Graceful degradation**: if optimistic is on (confirm returns instantly),
  the toggle still happens and the observe loop still judges by
  presence/distance; only the "command did not land" signal is lost. The
  integration must not break in that case.

## Diagnostics (StatusSensorEntity)

- `state`: `normal` | `healing` | `backoff`
- `iteration`: current heal iteration (0 when not healing)
- `discrepancy_age_sec`: age of the current discrepancy (0 if none)
- `last_heal_result`: `occupied` | `empty` | `gave_up` | `none`
- `backoff_left_sec`: seconds until heal may re-enter (0 if none)

## Edge cases

- **Genuinely still person > 15 min with distance never in range**: heal
  triggers, toggles, distance may fail to re-lock (Stop A not met), presence
  stays on (Stop B not met) -> iterate to cap -> backoff. Output stays `on`
  throughout (frozen on during HEALING, mirror on during BACKOFF), which is
  correct (the person is present). Accepted: bounded toggle burst every
  `HEAL_BACKOFF_SEC`; output never wrong.
- **Dead radar (no confirm, never recovers)**: confirms time out, Stop A/B
  never met -> cap -> backoff -> retry after `HEAL_BACKOFF_SEC`. Bounded
  mesh traffic. Output mirrors the (stuck) presence; a permanently broken
  unit needs manual intervention (power-cycle / re-pair).
- **HA restart mid-heal**: in-flight toggle task cancelled on
  `async_will_remove_from_hass`; park-fallback restores a select left at
  `off` within `PARK_FALLBACK_INTERVAL_SEC`.
- **presence flapping during observe**: if presence is ever `on` during the
  window, Stop B (whole-window off) cannot fire; if distance never enters
  range, Stop A cannot fire -> iterate. Correct.

## Testing (TDD)

`HealController` and the entity state machine, isolated with mocked hass /
service calls and a controllable clock:

- discrepancy timer: set/clear transitions; trigger at threshold; no trigger
  inside threshold; no trigger during backoff.
- confirmed toggle: off->confirm->on->confirm happy path; off-confirm
  timeout; on-confirm timeout (both: iteration still counts, no crash).
- Stop A: in_range+presence_on sustained `STOP_A_CONFIRM_SEC` -> occupied,
  output on, discrepancy cleared.
- Stop A debounce: single in_range sample shorter than confirm window does
  NOT stop.
- Stop B: whole observe window presence=off -> empty, output off.
- neither -> next iteration.
- iteration cap -> backoff; output unfreezes to mirror; re-entry suppressed
  until backoff expires; re-entry after backoff if discrepancy persists.
- output freeze during HEALING (phantom presence/distance ignored).
- park-fallback: restores non-`on` select to `on` when not healing; no-op /
  guarded during healing.
- optimistic-on degradation: instant confirm path still completes a heal.
- task cancellation on entity removal.

## Out of scope

- No config-flow UI; all parameters remain code constants.
- No change to device discovery / required DP suffixes (still need the
  `sensor` select, `presence`, `target_distance`, `detection_range`,
  `shield_range`).
