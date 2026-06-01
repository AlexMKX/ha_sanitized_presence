"""Constants for the Sanitized Presence integration."""

DOMAIN = "sanitized_presence"

# Config entry keys
CONF_POLL_INTERVAL = "poll_interval"

# Defaults / ranges
DEFAULT_POLL_S = 30
POLL_MIN_S = 5
POLL_MAX_S = 300

# Platforms
PLATFORMS = ["binary_sensor", "sensor"]

# Target hardware models (matched against device_registry model / model_id)
TARGET_MODELS = ("MTG075-ZB-RL", "MTG275-ZB-RL")

# Unique-id suffix strings used by Z2M for each required entity
SUFFIX_TARGET_DISTANCE = "target_distance"
SUFFIX_DETECTION_RANGE = "detection_range"
SUFFIX_SHIELD_RANGE = "shield_range"
SUFFIX_PRESENCE = "presence"  # Z2M DP key; HA device_class is "occupancy"

# Range evaluation
SHIELD_FLOOR_M = 0.1  # effective minimum even when shield_range=0

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

# Z2M select (operating mode) unique_id suffix.
SUFFIX_SENSOR = "sensor"

__all__ = [
    "DOMAIN",
    "CONF_POLL_INTERVAL",
    "DEFAULT_POLL_S",
    "POLL_MIN_S",
    "POLL_MAX_S",
    "PLATFORMS",
    "TARGET_MODELS",
    "SUFFIX_TARGET_DISTANCE",
    "SUFFIX_DETECTION_RANGE",
    "SUFFIX_SHIELD_RANGE",
    "SUFFIX_PRESENCE",
    "SHIELD_FLOOR_M",
    "DISCREPANCY_TRIGGER_SEC",
    "HEAL_OBSERVE_SEC",
    "HEAL_MAX_ITERATIONS",
    "HEAL_BACKOFF_SEC",
    "HEAL_CMD_TIMEOUT_SEC",
    "STOP_A_CONFIRM_SEC",
    "HEAL_POLL_SEC",
    "PARK_FALLBACK_INTERVAL_SEC",
    "SENSOR_RESET_SEQUENCE",
    "SUFFIX_SENSOR",
]
