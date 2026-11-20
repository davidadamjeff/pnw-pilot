"""Config flow: one entry holding the webhook id the comma uploads to."""
import secrets

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow

from .const import DOMAIN


class CommaLogsFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        if user_input is not None:
            return self.async_create_entry(title="comma logs", data={"webhook_id": user_input["webhook_id"]})
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required("webhook_id", default=secrets.token_hex(16)): str}),
        )
