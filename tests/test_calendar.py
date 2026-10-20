"""Calendar estimates, confirmed expiry dates and date-range behavior."""

from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.chemistwarehouse.calendar import PrescriptionCalendar, build_events
from custom_components.chemistwarehouse.parser import PrescriptionRepeat


def repeat(remaining=1):
    return PrescriptionRepeat("Example Medicine", "Test Patient", date(2024, 4, 12), remaining)


def medication(value):
    return {
        value.key: {
            "name": value.name,
            "status": "In Transit",
            "order_id": "1001",
            "ordered_at": "13/01/2024 8:37:19 AM",
            "quantity": 1,
            "shipment_statuses": ["In Transit"],
            "patient": value.patient,
        }
    }


@pytest.mark.parametrize(
    "remaining,expected",
    [
        (0, []),
        (1, [date(2024, 2, 27)]),
        (2, [date(2024, 2, 12), date(2024, 3, 13)]),
    ],
)
def test_remaining_repeats_are_spaced_before_expiry(remaining, expected):
    value = repeat(remaining)
    events = build_events((value,), medication(value))
    estimates = [event for event in events if event.summary.startswith("Estimated")]
    assert [event.start for event in estimates] == expected
    assert all(event.all_day for event in events)
    assert events[-1].start == date(2024, 4, 12)
    assert events[-1].end == date(2024, 4, 13)
    assert events[-1].summary == "Prescription expires: Example Medicine"
    assert all("not a pharmacy-confirmed" in event.description for event in estimates)


def test_missing_order_still_provides_actual_expiry():
    assert len(build_events((repeat(),), {})) == 1


def test_different_patient_order_cannot_anchor_estimates():
    other = PrescriptionRepeat("Example Medicine", "Other Patient", date(2024, 4, 12), 1)
    events = build_events((repeat(),), medication(other))
    assert not any(event.summary.startswith("Estimated") for event in events)


@pytest.mark.parametrize(
    "ordered_at", ["unknown", "12/04/2024 8:00:00 AM", "13/04/2024 8:00:00 AM"]
)
def test_invalid_or_too_late_order_does_not_create_estimates(ordered_at):
    value = repeat()
    meds = medication(value)
    meds[value.key]["ordered_at"] = ordered_at
    events = build_events((value,), meds)
    assert not any(event.summary.startswith("Estimated") for event in events)


def test_new_order_and_balance_recalculate_estimates():
    value = repeat(2)
    before = build_events((value,), medication(value))
    value = repeat(1)
    meds = medication(value)
    meds[value.key]["ordered_at"] = "12/02/2024 7:16:38 PM"
    after = build_events((value,), meds)
    assert len(before) == 4
    assert len(after) == 3
    assert after[1].start == date(2024, 3, 13)


async def test_calendar_filters_overlapping_ranges_and_excludes_end_boundary(monkeypatch):
    value = repeat()
    coordinator = SimpleNamespace(
        repeats=(value,), data=medication(value), last_update_success=True
    )
    calendar = PrescriptionCalendar(coordinator, "test")
    # Fix the local calendar zone independently of the execution host.
    from homeassistant.util import dt as dt_util

    monkeypatch.setattr(dt_util, "DEFAULT_TIME_ZONE", timezone.utc)
    start = datetime(2024, 2, 27, 12, tzinfo=timezone.utc)
    end = datetime(2024, 2, 28, tzinfo=timezone.utc)
    events = await calendar.async_get_events(Mock(), start, end)
    assert len(events) == 1
    assert events[0].start == date(2024, 2, 27)
    assert (
        await calendar.async_get_events(Mock(), end, datetime(2024, 2, 29, tzinfo=timezone.utc))
        == []
    )
    monkeypatch.setattr(dt_util, "now", lambda: end)
    assert calendar.event.summary.startswith("Prescription expires")
    coordinator.last_update_success = False
    assert calendar.event is None
    with pytest.raises(HomeAssistantError):
        await calendar.async_get_events(Mock(), start, end)


def test_order_status_and_details_are_available_without_repeats():
    value = repeat()
    meds = medication(value)
    events = build_events((), meds)
    assert len(events) == 1
    assert events[0].start == date(2024, 1, 13)
    assert events[0].summary == "Example Medicine: In Transit"
    assert "Order number: 1001" in events[0].description
    assert "Ordered quantity: 1" in events[0].description
    assert "Shipment statuses: In Transit" in events[0].description
    calendar = PrescriptionCalendar(
        SimpleNamespace(repeats=(), data=meds, last_update_success=True), "test"
    )
    assert calendar.extra_state_attributes["medications"][0]["status"] == "In Transit"
    assert "patient" not in calendar.extra_state_attributes["medications"][0]
    meds[value.key]["status"] = "Delivered"
    assert build_events((), meds)[0].summary == "Example Medicine: Delivered"
    assert build_events((), meds)[0].uid == events[0].uid


def test_order_details_are_in_repeat_and_expiry_events():
    value = repeat()
    meds = medication(value)
    meds[value.key].update(repeats_remaining=1, prescription_expires="2024-04-12")
    for event in build_events((value,), meds):
        assert "Current order status: In Transit" in event.description
        assert "Order number: 1001" in event.description
        assert "Repeats remaining: 1" in event.description
        assert "Test Patient" not in event.summary


async def test_upgrade_loads_only_calendar_and_removes_only_own_sensors():
    from unittest.mock import AsyncMock, patch

    from homeassistant.const import Platform

    from custom_components.chemistwarehouse import async_setup_entry

    hass = Mock()
    hass.config_entries.async_forward_entry_setups = AsyncMock()
    entry = Mock(entry_id="test", data={"username": "test", "password": "secret"})
    coordinator = Mock()
    coordinator.async_load_inventory = AsyncMock()
    registry = Mock()
    registered = [
        SimpleNamespace(domain="sensor", platform="chemistwarehouse", entity_id="sensor.old"),
        SimpleNamespace(domain="calendar", platform="chemistwarehouse", entity_id="calendar.keep"),
        SimpleNamespace(domain="sensor", platform="other", entity_id="sensor.other"),
    ]
    with (
        patch("custom_components.chemistwarehouse.async_create_clientsession"),
        patch("custom_components.chemistwarehouse.ChemistWarehouseClient"),
        patch(
            "custom_components.chemistwarehouse.ChemistWarehouseCoordinator",
            return_value=coordinator,
        ),
        patch("custom_components.chemistwarehouse.er.async_get", return_value=registry),
        patch(
            "custom_components.chemistwarehouse.er.async_entries_for_config_entry",
            return_value=registered,
        ),
    ):
        assert await async_setup_entry(hass, entry)
    hass.config_entries.async_forward_entry_setups.assert_awaited_once_with(
        entry, [Platform.CALENDAR]
    )
    registry.async_remove.assert_called_once_with("sensor.old")
