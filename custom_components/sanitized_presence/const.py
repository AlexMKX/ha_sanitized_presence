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

# Range evaluation
SHIELD_FLOOR_M = 0.1  # effective minimum even when shield_range=0

# Departure delay clamp applied to the live radar value
DELAY_MIN_S = 10
DELAY_MAX_S = 600
DEFAULT_DELAY_S = 60  # fallback when entity is unavailable

# Tick interval = departure_delay / 2, clamped to [TICK_FLOOR_S, TICK_CEILING_S]
TICK_FLOOR_S = 2
TICK_CEILING_S = 300

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
    "SHIELD_FLOOR_M",
    "DELAY_MIN_S",
    "DELAY_MAX_S",
    "DEFAULT_DELAY_S",
    "TICK_FLOOR_S",
    "TICK_CEILING_S",
]
