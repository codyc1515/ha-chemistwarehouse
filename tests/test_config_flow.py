"""Credential flows using Home Assistant's real ConfigFlow class."""

from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.core import HomeAssistant

from custom_components.chemistwarehouse.client import AuthenticationError, CannotConnect
from custom_components.chemistwarehouse.config_flow import ChemistWarehouseConfigFlow
from custom_components.chemistwarehouse.parser import ParseError


@pytest.mark.parametrize(
    "error,expected",
    [
        (AuthenticationError(), "invalid_auth"),
        (CannotConnect(), "cannot_connect"),
        (ParseError(), "unexpected_page"),
    ],
)
async def test_user_flow_errors(tmp_path, error, expected):
    flow = ChemistWarehouseConfigFlow()
    flow.hass = HomeAssistant(str(tmp_path))
    flow._validate = AsyncMock(side_effect=error)
    result = await flow.async_step_user({"username": "test", "password": "secret"})
    assert result["errors"] == {"base": expected}


async def test_reauth_preserves_username_and_updates_password(tmp_path):
    flow = ChemistWarehouseConfigFlow()
    flow.hass = HomeAssistant(str(tmp_path))
    entry = Mock(data={"username": "test", "password": "old"})
    flow._get_reauth_entry = Mock(return_value=entry)
    flow._validate = AsyncMock()
    flow.async_update_reload_and_abort = Mock(return_value={"type": "abort"})
    await flow.async_step_reauth_confirm({"password": "new"})
    flow._validate.assert_awaited_once_with({"username": "test", "password": "new"})
    flow.async_update_reload_and_abort.assert_called_once_with(
        entry, data_updates={"username": "test", "password": "new"}
    )
