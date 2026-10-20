"""Shared polling and persistent medication discovery."""

import logging
from asyncio import CancelledError
from contextlib import suppress
from dataclasses import asdict
from datetime import timedelta

from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import AuthenticationError, CannotConnect
from .const import DOMAIN, POLL_HOURS
from .parser import Medication, ParseError

_LOGGER = logging.getLogger(__name__)


class ChemistWarehouseCoordinator(DataUpdateCoordinator):
    """Fetch orders once per account and update prescription calendar data."""

    def __init__(self, hass, entry, client):
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=timedelta(hours=POLL_HOURS),
            # Repeat balances may change while medication statuses stay equal.
            always_update=True,
        )
        self.client = client
        self.store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.medications")
        self.inventory: dict[str, dict[str, dict]] = {}
        self.undiscovered_orders: tuple[str, ...] = ()
        self.repeats = ()
        self.data = {}
        self.last_update_success = False
        self.initial_refresh_task = None

    async def async_load_inventory(self):
        """Restore medication discovery without fetching remote calendar data."""
        self.inventory = await self.store.async_load() or {}

    async def async_shutdown(self):
        """Stop the initial refresh before its account session is closed."""
        await super().async_shutdown()
        if self.initial_refresh_task is not None:
            self.initial_refresh_task.cancel()
            with suppress(CancelledError):
                await self.initial_refresh_task
            self.initial_refresh_task = None

    async def _async_update_data(self):
        try:
            orders = await self.client.async_orders()
            data = {}
            inventory = {order_id: dict(items) for order_id, items in self.inventory.items()}
            undiscovered = []
            # Orders are newest first. Each patient and medication follows
            # its newest known prescription order.
            for order in orders:
                tracking = await self.client.async_tracking(order)
                invoice_medications = await self.client.async_medications(order)
                known = inventory.setdefault(order.order_id, {})
                current = {med.key: med for med in tracking.medications}
                for med in (*invoice_medications, *tracking.medications):
                    known[med.key] = asdict(med) | {"status": None}
                if not known:
                    undiscovered.append(order.order_id)
                for key, saved in known.items():
                    med = current.get(key, Medication(**saved))
                    # A known medication no longer listed as requiring action
                    # follows the progress bar. A generic banner is a fallback
                    # only when no item-specific action list is available.
                    status = med.status or (
                        "Action Required"
                        if tracking.action_required and not current
                        else tracking.status
                    )
                    data.setdefault(
                        key,
                        {
                            "name": med.name,
                            "patient": med.patient,
                            "quantity": med.quantity,
                            "status": status,
                            "order_id": order.order_id,
                            "ordered_at": order.ordered_at,
                            "shipment_statuses": list(tracking.shipment_statuses),
                        },
                    )
            if inventory != self.inventory:
                await self.store.async_save(inventory)
                self.inventory = inventory
            missing = tuple(undiscovered)
            if missing and missing != self.undiscovered_orders:
                _LOGGER.warning(
                    "%s prescription orders have no discoverable medication names; "
                    "the invoice and tracking pages may not identify their items",
                    len(missing),
                )
            self.undiscovered_orders = missing
            self.repeats = self.client.repeats
            for repeat in self.repeats:
                if repeat.key in data:
                    data[repeat.key]["repeats_remaining"] = repeat.remaining
                    data[repeat.key]["prescription_expires"] = repeat.expires.isoformat()
            return data
        except AuthenticationError as err:
            raise ConfigEntryAuthFailed("Chemist Warehouse credentials were rejected") from err
        except (CannotConnect, ParseError) as err:
            raise UpdateFailed(str(err)) from err
