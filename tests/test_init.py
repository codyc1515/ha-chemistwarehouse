"""Startup must not wait for remote calendar refreshes."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, HomeAssistant

from custom_components.chemistwarehouse import async_setup_entry, async_unload_entry
from custom_components.chemistwarehouse.calendar import PrescriptionCalendar
from custom_components.chemistwarehouse.client import AuthenticationError, CannotConnect
from custom_components.chemistwarehouse.coordinator import ChemistWarehouseCoordinator


@pytest.fixture
async def account(tmp_path):
    hass = HomeAssistant(str(tmp_path))
    entry = Mock(entry_id="test", data={"username": "test", "password": "secret"})
    entry.pref_disable_polling = False
    entry.async_create_background_task.side_effect = lambda hass, target, name: (
        hass.async_create_background_task(target, name)
    )
    client = SimpleNamespace(
        session=SimpleNamespace(close=AsyncMock()),
        async_orders=AsyncMock(return_value=[]),
        repeats=(),
    )
    coordinator = ChemistWarehouseCoordinator(hass, entry, client)
    coordinator.store.async_load = AsyncMock(return_value={})
    with (
        patch(
            "custom_components.chemistwarehouse.async_create_clientsession",
            return_value=client.session,
        ),
        patch("custom_components.chemistwarehouse.ChemistWarehouseClient", return_value=client),
        patch(
            "custom_components.chemistwarehouse.ChemistWarehouseCoordinator",
            return_value=coordinator,
        ),
        patch("custom_components.chemistwarehouse.er.async_get"),
        patch(
            "custom_components.chemistwarehouse.er.async_entries_for_config_entry",
            return_value=[],
        ),
    ):
        hass.config_entries = Mock()
        hass.config_entries.async_forward_entry_setups = AsyncMock()
        hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
        yield hass, entry, coordinator
    for call in entry.async_on_unload.call_args_list:
        # The coordinator's async shutdown is handled separately below.
        if call.args[0] != coordinator.async_shutdown:
            call.args[0]()
    await coordinator.async_shutdown()


async def test_startup_finishes_while_first_refresh_is_blocked(account):
    hass, entry, coordinator = account
    release = asyncio.Event()

    async def slow_orders():
        await release.wait()
        return []

    coordinator.client.async_orders.side_effect = slow_orders
    assert await async_setup_entry(hass, entry)
    await hass.async_block_till_done()
    coordinator.client.async_orders.assert_not_awaited()
    calendar = PrescriptionCalendar(coordinator, entry.entry_id)
    assert not calendar.available
    assert calendar.event is None
    assert calendar.extra_state_attributes == {}

    hass.state = CoreState.running
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await asyncio.wait_for(hass.async_block_till_done(), timeout=1)
    coordinator.client.async_orders.assert_awaited_once()
    assert not coordinator.initial_refresh_task.done()
    release.set()
    await coordinator.initial_refresh_task
    assert calendar.available
    assert coordinator.data == {}


async def test_setup_when_already_running_refreshes_in_background(account):
    hass, entry, coordinator = account
    hass.state = CoreState.running
    assert await async_setup_entry(hass, entry)
    await coordinator.initial_refresh_task
    coordinator.client.async_orders.assert_awaited_once()


async def test_unload_before_started_cancels_deferred_refresh(account):
    hass, entry, coordinator = account
    assert await async_setup_entry(hass, entry)
    assert await async_unload_entry(hass, entry)
    for call in entry.async_on_unload.call_args_list:
        if call.args[0] != coordinator.async_shutdown:
            call.args[0]()
    entry.async_on_unload.reset_mock()
    hass.state = CoreState.running
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    coordinator.client.async_orders.assert_not_awaited()
    coordinator.client.session.close.assert_awaited_once()


async def test_unload_cancels_running_refresh_before_closing_session(account):
    hass, entry, coordinator = account
    hass.state = CoreState.running
    cancelled = asyncio.Event()

    async def slow_orders():
        try:
            await asyncio.Event().wait()
        finally:
            assert coordinator.client.session.close.await_count == 0
            cancelled.set()

    coordinator.client.async_orders.side_effect = slow_orders
    assert await async_setup_entry(hass, entry)
    assert await async_unload_entry(hass, entry)
    assert cancelled.is_set()
    coordinator.client.session.close.assert_awaited_once()


async def test_first_network_failure_retries_and_recovers(account):
    hass, entry, coordinator = account
    hass.state = CoreState.running
    coordinator.client.async_orders.side_effect = CannotConnect("Offline")
    remove_listener = coordinator.async_add_listener(Mock())
    assert await async_setup_entry(hass, entry)
    await coordinator.initial_refresh_task
    assert not coordinator.last_update_success
    assert coordinator._unsub_refresh is not None
    coordinator.client.async_orders.side_effect = None
    await coordinator.async_refresh()
    assert coordinator.last_update_success
    remove_listener()


async def test_first_auth_failure_starts_reauthentication(account):
    hass, entry, coordinator = account
    hass.state = CoreState.running
    coordinator.client.async_orders.side_effect = AuthenticationError()
    assert await async_setup_entry(hass, entry)
    await coordinator.initial_refresh_task
    assert not coordinator.last_update_success
    entry.async_start_reauth_if_available.assert_called_once_with(hass)
