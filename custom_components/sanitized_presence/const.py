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

# Per-phase debounce between consecutive select_option calls in the reset
# cycle. The original pyscript used 0.5s (the empirical minimum for the
# Tuya MCU to accept each transition), but live observation showed
# edge-of-mesh radars still dropped intermediate phases and parked the
# select in 'unoccupied'. Bumped to 30s — matches RADAR_RESTART_DELAY so
# the full cycle pauses uniformly between every transition.
SENSOR_PHASE_DELAY_SEC = 30

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
]
