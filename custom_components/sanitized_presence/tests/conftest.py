"""Pytest fixtures for sanitized_presence tests."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from homeassistant.core import HomeAssistant


@pytest.fixture
def hass():
    """Minimal HA mock sufficient for unit-level tests."""
    # pylint: disable=redefined-outer-name
    mock = MagicMock(spec=HomeAssistant)
    mock.data = {}
    mock.states = MagicMock()
    mock.services = MagicMock()
    mock.config_entries = MagicMock()
    mock.async_create_task = MagicMock()
    return mock
