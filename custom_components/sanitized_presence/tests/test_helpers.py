"""Behavior tests for pure helpers used by the sanitized_presence integration.

Covers:
- _to_float: state-string conversion contract used everywhere in the
  decision logic. Wrong handling here flips the entire on/off behavior
  silently (e.g. "unavailable" would be parsed as a float and a stale
  radar reading would keep the sensor stuck on).
- _clamp: protects the integration from out-of-spec radar configuration
  (e.g. departure_delay=1s would make the deadline meaningless).
- in_range: the actual presence decision. Strict bounds and the
  shield_floor are what makes target_distance=0 ("no target") never
  trip the sensor.

How to run:
    pytest custom_components/sanitized_presence/tests/test_helpers.py
"""

from __future__ import annotations

import pytest

from custom_components.sanitized_presence.helpers import _clamp, _to_float, in_range


class TestToFloat:
    """_to_float must convert valid numerics and reject HA sentinel states."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("1.5", 1.5),
            ("3", 3.0),
            ("0", 0.0),
            ("-1.0", -1.0),
            (2.5, 2.5),
        ],
    )
    def test_returns_float_for_valid_numeric_inputs(self, value, expected):
        """Numeric inputs round-trip into floats.

        Validates: _to_float accepts numeric strings, ints, and floats.
        Code: custom_components/sanitized_presence/helpers.py::_to_float
        Assertion: returned value equals the float form of the input.
        Method:
        1. Arrange: parametrize numeric inputs covering int, float, signed.
        2. Act: call _to_float(value).
        3. Assert: result equals the expected float.
        """
        assert _to_float(value) == expected

    @pytest.mark.parametrize("sentinel", [None, "", "unknown", "unavailable"])
    def test_returns_none_for_ha_sentinels(self, sentinel):
        """HA "no value" sentinels must produce None, not a parse error.

        Validates: the integration must treat unknown/unavailable/None/""
        as "no data", which downstream _evaluate uses as the no-op signal.
        Code: custom_components/sanitized_presence/helpers.py::_to_float
        Assertion: _to_float returns None for every HA sentinel state.
        Method:
        1. Arrange: parametrize all HA sentinel states.
        2. Act: call _to_float(sentinel).
        3. Assert: result is None.
        """
        assert _to_float(sentinel) is None


class TestClamp:
    """_clamp must restrict departure_delay to safe bounds."""

    @pytest.mark.parametrize(
        ("value", "lo", "hi", "expected"),
        [
            (30.0, 10.0, 600.0, 30.0),  # within
            (1.0, 10.0, 600.0, 10.0),  # below
            (999.0, 10.0, 600.0, 600.0),  # above
            (10.0, 10.0, 600.0, 10.0),  # at lower bound
            (600.0, 10.0, 600.0, 600.0),  # at upper bound
        ],
    )
    def test_clamps_to_inclusive_bounds(self, value, lo, hi, expected):
        """_clamp returns inclusive boundary values verbatim.

        Validates: callers that depend on lo and hi being reachable
        (e.g. _evaluate using DELAY_MIN_S=10 directly when delay<10).
        Code: custom_components/sanitized_presence/helpers.py::_clamp
        Assertion: result is exactly lo when below, hi when above, value
            when in-range, and exactly the boundary at the boundary.
        Method:
        1. Arrange: parametrize value across all 5 regions.
        2. Act: call _clamp(value, lo, hi).
        3. Assert: equals expected.
        """
        assert _clamp(value, lo, hi) == expected


class TestInRange:
    """in_range encodes the presence decision shield_floor < target < detect."""

    def test_target_in_window_returns_true(self):
        """A target strictly inside the window means "present".

        Validates: the happy path that the sanitized binary sensor will
        pulse on when someone is within the radar's configured cone.
        Code: custom_components/sanitized_presence/helpers.py::in_range
        Assertion: in_range(1.5, shield=0, detect=4.5, floor=0.1) is True.
        Method:
        1. Arrange: a target at 1.5 m, shield=0, detect=4.5, floor=0.1.
        2. Act: call in_range.
        3. Assert: returns True.
        """
        assert in_range(1.5, 0.0, 4.5, 0.1) is True

    def test_target_zero_is_rejected_via_shield_floor(self):
        """target_distance=0 must never trip the sensor.

        Validates: protection against the radar reporting target=0
        ("no target") which would otherwise be inside [0, detect].
        Regression context: this is the latch scenario that motivates the
        whole integration.
        Code: custom_components/sanitized_presence/helpers.py::in_range
        Assertion: target=0 with shield=0 and floor=0.1 returns False.
        Method:
        1. Arrange: target=0, shield=0, detect=4.5, floor=0.1.
        2. Act: call in_range.
        3. Assert: returns False because effective_min = max(0, 0.1) = 0.1.
        """
        assert in_range(0.0, 0.0, 4.5, 0.1) is False

    @pytest.mark.parametrize(
        ("target", "expected"),
        [
            (0.1, False),  # equals floor, strict
            (0.11, True),  # just above floor
            (4.49, True),  # just below detect
            (4.5, False),  # equals detect, strict
            (5.0, False),  # above detect
            (-1.0, False),  # negative
        ],
    )
    def test_strict_boundary_semantics(self, target, expected):
        """Both bounds are strict (open interval).

        Validates: a target at exactly the configured limit is not
        considered present. This matches occupancy_reset.py's range
        check `shield < target < detection`.
        Code: custom_components/sanitized_presence/helpers.py::in_range
        Assertion: in_range applies < and not <= on both ends.
        Method:
        1. Arrange: parametrize targets at and around both boundaries.
        2. Act: call in_range with fixed shield=0, detect=4.5, floor=0.1.
        3. Assert: matches expected, in particular False at floor and detect.
        """
        assert in_range(target, 0.0, 4.5, 0.1) is expected

    def test_shield_dominates_over_floor(self):
        """Explicit shield_range beats the safety floor.

        Validates: user-configured shield (radar blind zone) takes
        precedence over the safety floor when shield > floor.
        Code: custom_components/sanitized_presence/helpers.py::in_range
        Assertion: target between floor and shield is rejected; target
            above shield is accepted.
        Method:
        1. Arrange: shield=1.0, floor=0.1.
        2. Act: in_range with target=0.5 then target=1.5.
        3. Assert: False then True (effective_min = max(1.0, 0.1) = 1.0).
        """
        assert in_range(0.5, 1.0, 4.5, 0.1) is False
        assert in_range(1.5, 1.0, 4.5, 0.1) is True
