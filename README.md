# Sanitized Presence for Home Assistant

Creates a reliable `binary_sensor.*_sanitized_presence` for every
Tuya MTG075-ZB-RL / MTG275-ZB-RL mmWave radar, derived from the
numeric `target_distance` measurement rather than the radar's
occasionally-latching boolean presence datapoint.

## Installation (HACS)

1. HACS → three-dot menu → **Custom repositories**
2. Add `https://github.com/AlexMKX/ha_sanitized_presence`, category **Integration**
3. Search **Sanitized Presence** in HACS and install
4. Restart Home Assistant
5. Settings → Devices & services → Add integration → **Sanitized Presence**

## Configuration

- `poll_interval` (seconds, 5..300): how often to scan for new radar devices.

## How it works

For each discovered MTG075/MTG275 device the integration reads:
- `sensor.*_target_distance` — current radar measurement
- `number.*_detection_range` — configured max detection distance
- `number.*_shield_range` — configured blind zone (min distance)
- `number.*_departure_delay` — how long to keep presence on after target leaves range

`sanitized_presence` is `on` when `max(shield_range, 0.1 m) < target_distance < detection_range`.
A sliding-window timer (length = `departure_delay`, clamped to 10..600 s) keeps it `on`
as long as the target stays in range; it goes `off` when the timer expires.

A companion `sensor.*_sanitized_presence_deadline` shows the expiry datetime on the device card.
