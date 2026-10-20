"""Check persistence, latest-order selection, and failure behavior in HA."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from test_parser import progress

from custom_components.chemistwarehouse.client import AuthenticationError
from custom_components.chemistwarehouse.coordinator import ChemistWarehouseCoordinator
from custom_components.chemistwarehouse.parser import (
    Medication,
    Order,
    ParseError,
    PrescriptionRepeat,
    Tracking,
    parse_invoice,
    parse_orders,
    parse_tracking,
)


@pytest.fixture
async def coordinator(tmp_path):
    hass = HomeAssistant(str(tmp_path))
    entry = Mock(entry_id="test-entry")
    client = SimpleNamespace(
        async_orders=AsyncMock(),
        async_tracking=AsyncMock(),
        async_medications=AsyncMock(return_value=()),
        repeats=(),
    )
    coordinator = ChemistWarehouseCoordinator(hass, entry, client)
    coordinator.store = SimpleNamespace(
        async_load=AsyncMock(return_value=None), async_save=AsyncMock()
    )
    await coordinator.async_load_inventory()
    yield coordinator
    await coordinator.async_shutdown()


async def test_medication_survives_disappearing_action_list_and_restart(
    coordinator, pages, tmp_path
):
    order = parse_orders(pages["account"])[0]
    coordinator.client.async_orders.return_value = [order]
    coordinator.client.async_tracking.return_value = parse_tracking(pages["tracking"], "1001")
    initial = await coordinator._async_update_data()
    key = next(iter(initial))
    assert initial[key]["status"] == "Prescription Required"
    cached = coordinator.store.async_save.await_args.args[0]
    assert cached["1001"][key]["status"] is None

    restored = ChemistWarehouseCoordinator(
        coordinator.hass, Mock(entry_id="test-entry"), coordinator.client
    )
    restored.store = SimpleNamespace(
        async_load=AsyncMock(return_value=cached), async_save=AsyncMock()
    )
    await restored.async_load_inventory()
    coordinator.client.async_tracking.return_value = parse_tracking(
        progress(pages["tracking"], 3), "1001"
    )
    updated = await restored._async_update_data()
    assert updated[key]["status"] == "In Transit"
    assert updated[key]["name"] == initial[key]["name"]
    await restored.async_shutdown()


async def test_latest_order_wins_for_existing_medication(coordinator):
    med = Medication("Medicine", "Patient", "1", "Prescription Required")
    recent = Order("2", "", "21/08/2023 4:26:39 PM", "")
    old = Order("1", "", "19/08/2023 11:07:14 AM", "")
    coordinator.client.async_orders.return_value = [recent, old]
    coordinator.client.async_tracking.side_effect = [
        Tracking("Pending Confirmation", (med,), ("Pending Confirmation",), True),
        Tracking("Delivered", (Medication("Medicine", "Patient", "2"),), ("Delivered",), False),
    ]
    data = await coordinator._async_update_data()
    assert len(data) == 1
    assert data[med.key]["order_id"] == "2"
    assert data[med.key]["status"] == "Prescription Required"


async def test_first_install_discovers_processing_medication_from_invoice(coordinator, pages):
    coordinator.client.async_orders.return_value = parse_orders(pages["account"])
    coordinator.client.async_tracking.return_value = parse_tracking(
        progress(pages["tracking"], 1), "1001"
    )
    coordinator.client.async_medications.return_value = parse_invoice(pages["invoice"], "1001")
    data = await coordinator._async_update_data()
    assert len(data) == 1
    assert next(iter(data.values()))["status"] == "Processing order"
    assert coordinator.undiscovered_orders == ()


async def test_item_requirement_overrides_invoice_and_order_status(coordinator, pages):
    coordinator.client.async_orders.return_value = parse_orders(pages["account"])
    coordinator.client.async_tracking.return_value = parse_tracking(pages["tracking"], "1001")
    coordinator.client.async_medications.return_value = parse_invoice(pages["invoice"], "1001")
    data = await coordinator._async_update_data()
    assert len(data) == 1
    assert next(iter(data.values()))["status"] == "Prescription Required"


@pytest.mark.parametrize(
    "exception,expected",
    [(AuthenticationError(), ConfigEntryAuthFailed), (ParseError("Changed markup"), UpdateFailed)],
)
async def test_failed_refresh_does_not_commit_partial_inventory(
    coordinator, pages, exception, expected
):
    coordinator.client.async_orders.return_value = parse_orders(pages["account"])
    coordinator.client.async_tracking.side_effect = exception
    with pytest.raises(expected):
        await coordinator._async_update_data()
    assert coordinator.inventory == {}
    coordinator.store.async_save.assert_not_awaited()


async def test_unknown_inventory_warns_without_inventing_medications(coordinator, pages):
    coordinator.client.async_orders.return_value = parse_orders(pages["account"])
    coordinator.client.async_tracking.return_value = parse_tracking(
        progress(pages["tracking"], 4), "1001"
    )
    assert await coordinator._async_update_data() == {}
    assert coordinator.undiscovered_orders == ("1001",)


async def test_repeat_balance_updates_calendar_data(coordinator, pages):
    from datetime import date

    order = parse_orders(pages["account"])[0]
    coordinator.client.async_orders.return_value = [order]
    coordinator.client.async_tracking.return_value = parse_tracking(pages["tracking"], "1001")
    repeat = PrescriptionRepeat(
        "Example Medicine 10mg 30 tablets", "Test Patient", date(2024, 7, 16), 2
    )
    coordinator.client.repeats = (repeat,)
    data = await coordinator._async_update_data()
    assert data[repeat.key]["repeats_remaining"] == 2
    assert data[repeat.key]["prescription_expires"] == "2024-07-16"
    coordinator.client.repeats = ()
    data = await coordinator._async_update_data()
    assert coordinator.repeats == ()
    assert "repeats_remaining" not in data[repeat.key]
