"""Chemist Warehouse prescription integration."""

from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.storage import Store

from .client import ChemistWarehouseClient
from .const import DOMAIN
from .coordinator import ChemistWarehouseCoordinator

PLATFORMS = [Platform.CALENDAR]


async def async_setup_entry(hass, entry):
    """Set up a private account session and its coordinator."""
    session = async_create_clientsession(hass)
    client = ChemistWarehouseClient(session, entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD])
    coordinator = ChemistWarehouseCoordinator(hass, entry, client)
    try:
        await coordinator.async_config_entry_first_refresh()
        entry.runtime_data = coordinator
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        # The calendar replaces the medication sensors from earlier versions.
        registry = er.async_get(hass)
        for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
            if entity.domain == "sensor" and entity.platform == DOMAIN:
                registry.async_remove(entity.entity_id)
    except BaseException:
        await coordinator.async_shutdown()
        await session.close()
        raise
    return True


async def async_unload_entry(hass, entry):
    """Unload entities and release the authenticated session."""
    if unloaded := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.client.session.close()
    return unloaded


async def async_remove_entry(hass, entry):
    """Delete locally cached medication data when the account is removed."""
    await Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.medications").async_remove()
