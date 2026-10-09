"""UI setup and credential reauthentication."""

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .client import AuthenticationError, CannotConnect, ChemistWarehouseClient
from .const import DOMAIN, NAME
from .parser import ParseError


class ChemistWarehouseConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Validate credentials before storing a config entry."""

    VERSION = 1

    async def _validate(self, user_input):
        session = async_create_clientsession(self.hass)
        try:
            await ChemistWarehouseClient(
                session, user_input[CONF_USERNAME], user_input[CONF_PASSWORD]
            ).async_login()
        finally:
            await session.close()

    async def async_step_user(self, user_input=None):
        return await self._credentials("user", user_input)

    async def async_step_reauth(self, entry_data):
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        return await self._credentials("reauth_confirm", user_input)

    async def _credentials(self, step, user_input):
        errors = {}
        if user_input is not None:
            user_input = dict(user_input)
            if step == "reauth_confirm":
                # Preserve the account identity and its medication entities.
                user_input[CONF_USERNAME] = self._get_reauth_entry().data[CONF_USERNAME]
            else:
                user_input[CONF_USERNAME] = user_input[CONF_USERNAME].strip()
            try:
                await self._validate(user_input)
            except AuthenticationError:
                errors["base"] = "invalid_auth"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except ParseError:
                errors["base"] = "unexpected_page"
            else:
                if step == "reauth_confirm":
                    return self.async_update_reload_and_abort(
                        self._get_reauth_entry(), data_updates=user_input
                    )
                await self.async_set_unique_id(user_input[CONF_USERNAME].casefold())
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=NAME, data=user_input)
        schema = {vol.Required(CONF_PASSWORD): str}
        if step == "user":
            schema = {vol.Required(CONF_USERNAME): str, **schema}
        return self.async_show_form(step_id=step, data_schema=vol.Schema(schema), errors=errors)
