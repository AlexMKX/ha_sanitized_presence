"""Config flow for Sanitized Presence."""

from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback

from .const import CONF_POLL_INTERVAL, DEFAULT_POLL_S, DOMAIN, POLL_MAX_S, POLL_MIN_S


def _schema(current_poll: int = DEFAULT_POLL_S) -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional(CONF_POLL_INTERVAL, default=current_poll): vol.All(
                vol.Coerce(int), vol.Range(min=POLL_MIN_S, max=POLL_MAX_S)
            ),
        }
    )


class SanitizedPresenceConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Sanitized Presence."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        if user_input is not None:
            return self.async_create_entry(
                title="Sanitized Presence",
                data={
                    CONF_POLL_INTERVAL: user_input.get(CONF_POLL_INTERVAL, DEFAULT_POLL_S),
                },
            )
        return self.async_show_form(step_id="user", data_schema=_schema())

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return SanitizedPresenceOptionsFlow()


class SanitizedPresenceOptionsFlow(config_entries.OptionsFlow):
    """Options flow for Sanitized Presence."""

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            new_data = dict(self.config_entry.data)
            new_data[CONF_POLL_INTERVAL] = user_input[CONF_POLL_INTERVAL]
            self.hass.config_entries.async_update_entry(self.config_entry, data=new_data)
            await self.hass.config_entries.async_reload(self.config_entry.entry_id)
            return self.async_create_entry(title="", data={})

        return self.async_show_form(
            step_id="init",
            data_schema=_schema(
                current_poll=self.config_entry.data.get(CONF_POLL_INTERVAL, DEFAULT_POLL_S)
            ),
        )
