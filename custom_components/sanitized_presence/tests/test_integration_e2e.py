"""End-to-end smoke tests for sanitized_presence in a containerized HA.

These tests are tagged with the project-wide `docker_e2e` marker (see
pyproject.toml). They require a real Home Assistant instance to be
running and accessible through the `ha_instance` fixture supplied by the
project's test harness. CI runs them in a separate stage so unit tests
remain fast.

How to run (opt-in):
    pytest -m docker_e2e custom_components/sanitized_presence/tests/test_integration_e2e.py
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.docker_e2e


@pytest.mark.asyncio
class TestSanitizedPresenceE2E:
    """End-to-end: integration can be installed in a real HA instance."""

    async def test_integration_install_creates_exactly_one_entry(self, ha_instance):
        """Adding the integration leaves exactly one config entry.

        Validates: the manifest, config_flow, single_instance_allowed
        guard, and `async_setup_entry` all wire up cleanly in a real HA
        instance. If any of them break, HA either rejects the flow or
        creates duplicate/zero entries.
        Code: custom_components/sanitized_presence/__init__.py::async_setup_entry,
              custom_components/sanitized_presence/config_flow.py::SanitizedPresenceConfigFlow
        Assertion: the add_integration result is either create_entry
            (first install) or abort with a known reason
            (already_configured / single_instance_allowed); and
            get_config_entries returns exactly one entry afterwards.
        Method:
        1. Arrange: rely on the containerized HA from ha_instance fixture.
        2. Act: call ha_instance.add_integration("sanitized_presence",
            {"poll_interval": 30}).
        3. Assert: result type is in {create_entry, abort}; if abort,
            reason is in the documented set; one entry exists.
        """
        result = await ha_instance.add_integration(
            "sanitized_presence",
            {"poll_interval": 30},
        )
        if result.get("type") == "create_entry":
            assert result.get("title") == "Sanitized Presence"
        elif result.get("type") == "abort":
            assert result.get("reason") in {
                "already_configured",
                "single_instance_allowed",
            }
        else:
            raise AssertionError(f"Unexpected flow result: {result}")

        entries = await ha_instance.get_config_entries("sanitized_presence")
        assert len(entries) == 1
