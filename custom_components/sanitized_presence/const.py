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
SUFFIX_DEPARTURE_DELAY = "departure_delay"
SUFFIX_PRESENCE = "presence"  # Z2M DP key; HA device_class is "occupancy"

# Range evaluation
SHIELD_FLOOR_M = 0.1  # effective minimum even when shield_range=0

# Departure delay clamp applied to the live radar value
DELAY_MIN_S = 10
DELAY_MAX_S = 600
DEFAULT_DELAY_S = 60  # fallback when entity is unavailable

# Tick interval = departure_delay / 2, clamped to [TICK_FLOOR_S, TICK_CEILING_S]
TICK_FLOOR_S = 2
TICK_CEILING_S = 300

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
    "SUFFIX_DEPARTURE_DELAY",
    "SUFFIX_PRESENCE",
    "SHIELD_FLOOR_M",
    "DELAY_MIN_S",
    "DELAY_MAX_S",
    "DEFAULT_DELAY_S",
    "TICK_FLOOR_S",
    "TICK_CEILING_S",
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
