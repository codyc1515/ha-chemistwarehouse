"""Prescription expiry dates and evenly spaced estimated repeat reminders."""

from datetime import datetime, timedelta

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

PARALLEL_UPDATES = 0


def medication_details(medication) -> str:
    """Include the former sensor's information in calendar event descriptions."""
    if not medication:
        return ""
    lines = []
    for key, label in (
        ("status", "Current order status"),
        ("order_id", "Order number"),
        ("ordered_at", "Ordered at (New Zealand time)"),
        ("quantity", "Ordered quantity"),
        ("repeats_remaining", "Repeats remaining"),
        ("prescription_expires", "Prescription expires"),
    ):
        if medication.get(key) is not None:
            lines.append(f"{label}: {medication[key]}")
    if medication.get("shipment_statuses"):
        lines.append("Shipment statuses: " + ", ".join(medication["shipment_statuses"]))
    return "\n".join(lines)


def build_events(repeats, medications) -> list[CalendarEvent]:
    """Space N reminders at k/(N+1) between latest order and expiry dates."""
    events = []
    for key, medication in medications.items():
        try:
            ordered = datetime.strptime(medication["ordered_at"], "%d/%m/%Y %I:%M:%S %p").date()
        except (KeyError, ValueError):
            continue
        events.append(
            CalendarEvent(
                start=ordered,
                end=ordered + timedelta(days=1),
                summary=f"{medication['name']}: {medication['status']}",
                description=medication_details(medication),
                uid=f"{key}_order_{medication['order_id']}",
            )
        )
    for repeat in repeats:
        medication = medications.get(repeat.key)
        details = medication_details(medication)
        events.append(
            CalendarEvent(
                start=repeat.expires,
                end=repeat.expires + timedelta(days=1),
                summary=f"Prescription expires: {repeat.name}",
                description=f"Expiry date reported by Chemist Warehouse Online. "
                f"Repeats remaining: {repeat.remaining}.\n\n{details}",
                uid=f"{repeat.key}_{repeat.expires.isoformat()}_expiry",
            )
        )
        if not medication or repeat.remaining == 0:
            continue
        try:
            ordered = datetime.strptime(medication["ordered_at"], "%d/%m/%Y %I:%M:%S %p").date()
        except (KeyError, ValueError):
            continue
        span = (repeat.expires - ordered).days
        if span <= 0:
            continue
        for index in range(1, repeat.remaining + 1):
            due = ordered + timedelta(days=span * index // (repeat.remaining + 1))
            # Keep estimates strictly between the order date and expiry.
            if due <= ordered or due >= repeat.expires:
                continue
            events.append(
                CalendarEvent(
                    start=due,
                    end=due + timedelta(days=1),
                    summary=f"Estimated repeat: {repeat.name} ({index} of {repeat.remaining})",
                    description="Estimated planning reminder, not a pharmacy-confirmed dispensing "
                    "or eligibility date. Calculated by evenly spacing the remaining repeats "
                    f"between the latest known order date ({ordered.isoformat()}) and prescription "
                    f"expiry ({repeat.expires.isoformat()}). "
                    f"Repeats remaining: {repeat.remaining}. "
                    "Estimates recalculate when the latest order or repeat balance changes."
                    f"\n\n{details}",
                    uid=f"{repeat.key}_{repeat.expires.isoformat()}_repeat_{index}",
                )
            )
    return sorted(events, key=lambda event: (event.start, event.summary))


async def async_setup_entry(hass, entry, async_add_entities):
    """Add one read-only prescription calendar per account."""
    async_add_entities([PrescriptionCalendar(entry.runtime_data, entry.entry_id)])


class PrescriptionCalendar(CoordinatorEntity, CalendarEntity):
    """Expose prescription orders, repeat estimates and expiry dates."""

    _attr_name = "Chemist Warehouse prescriptions"
    _attr_icon = "mdi:calendar-heart"

    def __init__(self, coordinator, entry_id):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry_id}_prescriptions"

    @property
    def extra_state_attributes(self):
        """Keep current medication information accessible after order events pass."""
        if not self.available:
            return {}
        fields = (
            "name",
            "status",
            "order_id",
            "ordered_at",
            "quantity",
            "shipment_statuses",
            "repeats_remaining",
            "prescription_expires",
        )
        return {
            "medications": [
                {field: med[field] for field in fields if field in med}
                for med in self.coordinator.data.values()
            ]
        }

    @property
    def event(self):
        if not self.available:
            return None
        now = dt_util.now()
        return next(
            (
                event
                for event in build_events(self.coordinator.repeats, self.coordinator.data)
                if event.end_datetime_local > now
            ),
            None,
        )

    async def async_get_events(self, hass, start_date, end_date):
        if not self.available:
            raise HomeAssistantError("Prescription calendar is unavailable")
        return [
            event
            for event in build_events(self.coordinator.repeats, self.coordinator.data)
            if event.start_datetime_local < end_date and event.end_datetime_local > start_date
        ]
