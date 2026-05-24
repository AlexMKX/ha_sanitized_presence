"""Behavior tests for Sanitized Presence config flow and options flow.

Covers:
- Initial setup step shows a form containing poll_interval.
- Submitting an empty form creates an entry with the documented defaults.
- Submitting an explicit poll_interval creates an entry with that value.
- Options flow updates the existing config entry's data on submit.

How to run:
    pytest custom_components/sanitized_presence/tests/test_config_flow.py
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.sanitized_presence.config_flow import (
    SanitizedPresenceConfigFlow,
    SanitizedPresenceOptionsFlow,
)
from custom_components.sanitized_presence.const import CONF_POLL_INTERVAL, DEFAULT_POLL_S


class TestSanitizedPresenceConfigFlow:
    """Initial setup wizard: form rendering and entry creation."""

    @pytest.fixture
    def flow(self):
        flow = SanitizedPresenceConfigFlow()
        flow.hass = MagicMock()
        flow.hass.config_entries = MagicMock()
        return flow

    async def test_initial_step_renders_form_with_poll_interval(self, flow):
        """The user step renders a voluptuous form containing poll_interval.

        Validates: the only configurable knob is exposed in the wizard.
        If this regressed (e.g., schema renamed), users would never be
        able to change discovery cadence from the UI.
        Code: custom_components/sanitized_presence/config_flow.py::SanitizedPresenceConfigFlow.async_step_user
        Assertion: returned step is type="form", id="user", schema
            contains CONF_POLL_INTERVAL.
        Method:
        1. Arrange: stub unique-id checks; pass user_input=None.
        2. Act: await async_step_user.
        3. Assert: type/step_id/schema contents.
        """
        with (
            patch.object(flow, "async_set_unique_id", new_callable=AsyncMock),
            patch.object(flow, "_abort_if_unique_id_configured"),
        ):
            result = await flow.async_step_user(user_input=None)

        assert result["type"] == "form"
        assert result["step_id"] == "user"
        assert CONF_POLL_INTERVAL in result["data_schema"].schema

    async def test_empty_submission_creates_entry_with_default_poll(self, flow):
        """Submitting an empty form creates an entry with DEFAULT_POLL_S.

        Validates: the documented default reaches the persisted entry,
        so a user who clicks "submit" without changing anything still
        gets a working integration.
        Code: custom_components/sanitized_presence/config_flow.py::SanitizedPresenceConfigFlow.async_step_user
        Assertion: async_create_entry is called with title "Sanitized
            Presence" and data[CONF_POLL_INTERVAL] == DEFAULT_POLL_S.
        Method:
        1. Arrange: stub unique-id checks and async_create_entry.
        2. Act: await async_step_user with user_input={}.
        3. Assert: create-entry kwargs match defaults.
        """
        with (
            patch.object(flow, "async_set_unique_id", new_callable=AsyncMock),
            patch.object(flow, "_abort_if_unique_id_configured"),
            patch.object(flow, "async_create_entry") as mock_create,
        ):
            mock_create.return_value = {"type": "create_entry"}
            await flow.async_step_user(user_input={})

        kwargs = mock_create.call_args[1]
        assert kwargs["title"] == "Sanitized Presence"
        assert kwargs["data"][CONF_POLL_INTERVAL] == DEFAULT_POLL_S

    async def test_explicit_poll_interval_is_persisted(self, flow):
        """A user-provided poll_interval is persisted verbatim.

        Validates: the wizard does not silently drop or override the
        user's input.
        Code: custom_components/sanitized_presence/config_flow.py::SanitizedPresenceConfigFlow.async_step_user
        Assertion: data[CONF_POLL_INTERVAL] == 60 when user inputs 60.
        Method:
        1. Arrange: stub helpers; user_input={CONF_POLL_INTERVAL: 60}.
        2. Act: await async_step_user.
        3. Assert: data carried over.
        """
        with (
            patch.object(flow, "async_set_unique_id", new_callable=AsyncMock),
            patch.object(flow, "_abort_if_unique_id_configured"),
            patch.object(flow, "async_create_entry") as mock_create,
        ):
            mock_create.return_value = {"type": "create_entry"}
            await flow.async_step_user(user_input={CONF_POLL_INTERVAL: 60})

        kwargs = mock_create.call_args[1]
        assert kwargs["data"][CONF_POLL_INTERVAL] == 60


class TestSanitizedPresenceOptionsFlow:
    """Options flow updates poll_interval on an existing config entry."""

    @pytest.fixture
    def flow(self):
        config_entry = MagicMock()
        config_entry.entry_id = "entry_1"
        config_entry.data = {CONF_POLL_INTERVAL: 30}
        flow = SanitizedPresenceOptionsFlow()
        flow.hass = MagicMock()
        # config_entry is a read-only property in HA 2026; _config_entry_id
        # returns self.handler, which is a mutable class-level attribute.
        flow.handler = "entry_1"

        def _update_entry(entry, data=None, **kwargs):
            if data is not None:
                entry.data.update(data)

        flow.hass.config_entries.async_get_known_entry.return_value = config_entry
        flow.hass.config_entries.async_update_entry.side_effect = _update_entry
        flow.hass.config_entries.async_reload = AsyncMock()
        return flow

    async def test_init_step_renders_form(self, flow):
        """Opening the options page returns a form, not a completion.

        Validates: the user sees an editable form (rather than an
        immediate finish) when no input is supplied.
        Code: custom_components/sanitized_presence/config_flow.py::SanitizedPresenceOptionsFlow.async_step_init
        Assertion: result["type"] == "form".
        Method:
        1. Arrange: options flow attached to an entry with poll=30.
        2. Act: await async_step_init(user_input=None).
        3. Assert: type is "form".
        """
        result = await flow.async_step_init(user_input=None)
        assert result["type"] == "form"

    async def test_submitting_options_updates_entry_data(self, flow):
        """Submitting a new poll_interval mutates the entry's data dict.

        Validates: changes from the options UI propagate to the stored
        config entry so the manager picks up the new value after reload.
        Code: custom_components/sanitized_presence/config_flow.py::SanitizedPresenceOptionsFlow.async_step_init
        Assertion: flow.config_entry.data[CONF_POLL_INTERVAL] == 60 after
            submission.
        Method:
        1. Arrange: starting entry data has poll=30.
        2. Act: await async_step_init(user_input={CONF_POLL_INTERVAL: 60}).
        3. Assert: entry.data was updated to 60.
        """
        with patch.object(flow, "async_create_entry") as mock_create:
            mock_create.return_value = {"type": "create_entry"}
            await flow.async_step_init(user_input={CONF_POLL_INTERVAL: 60})

        assert flow.config_entry.data[CONF_POLL_INTERVAL] == 60
