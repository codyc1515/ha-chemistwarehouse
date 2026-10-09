"""Exercise observed markup and synthetic later lifecycle states."""

import pytest
from bs4 import BeautifulSoup

from custom_components.chemistwarehouse.const import STATUSES
from custom_components.chemistwarehouse.parser import (
    Medication,
    ParseError,
    is_login,
    login_form,
    parse_orders,
    parse_tracking,
)


def progress(html, index):
    soup = BeautifulSoup(html, "html.parser")
    for i, step in enumerate(soup.select(".track-progress li")):
        label = step.select_one(".status_red, .status_green, .status")
        label["class"] = ["status_green" if i <= index else "status"]
    for node in soup.select(
        ".prescription-tracking__action-banner, .prescription-confirmation-items"
    ):
        node.decompose()
    return str(soup)


def test_login_preserves_fresh_webforms_fields(pages):
    action, fields = login_form(pages["login"], "test@example.test", "p&'word")
    assert action.startswith("https://www.chemistwarehouse.co.nz/login?")
    assert fields["__VIEWSTATE"] == "test-__VIEWSTATE"
    assert fields["__EVENTVALIDATION"] == "test-__EVENTVALIDATION"
    assert fields["p$lt$ctl08$pageplaceholder$p$lt$ctl00$wAMSLogon$password1"] == "p&'word"
    assert fields["p$lt$ctl08$pageplaceholder$p$lt$ctl00$wAMSLogon$Signin"] == "SIGN IN"


def test_only_prescription_orders(pages):
    orders = parse_orders(pages["account"])
    assert len(orders) == 1
    assert orders[0].order_id == "1001"
    assert orders[0].ordered_at == "17/04/2024 9:18:52 AM"


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/track-your-order?o=1001",
        "https://www.chemistwarehouse.co.nz.evil.example/track-your-order?o=1001",
        "http://www.chemistwarehouse.co.nz/track-your-order?o=1001",
        "/track-your-order-other?o=1001",
        "/c/tracking?o=1001",
    ],
)
def test_excludes_other_tracking_urls(pages, url):
    soup = BeautifulSoup(pages["account"], "html.parser")
    soup.select_one('a[href*="track-your-order"]')["href"] = url
    assert parse_orders(str(soup)) == []


def test_observed_prescription_requirement(pages):
    tracking = parse_tracking(pages["tracking"], "1001")
    assert tracking.status == "Pending Confirmation"
    assert tracking.action_required
    assert tracking.medications == (
        Medication(
            "Example Medicine 10mg 30 tablets", "Test Patient", "1", "Prescription Required"
        ),
    )


@pytest.mark.parametrize("index,expected", list(enumerate(STATUSES[2:])))
def test_later_progress_states(pages, index, expected):
    tracking = parse_tracking(progress(pages["tracking"], index), "1001")
    assert tracking.status == expected
    assert not tracking.medications
    assert not tracking.action_required


def test_split_shipments_are_not_prematurely_delivered(pages):
    tracking = parse_tracking(
        progress(pages["tracking"], 4) + progress(pages["tracking"], 3), "1001"
    )
    assert tracking.status == "In Transit"
    assert tracking.shipment_statuses == ("Delivered", "In Transit")


def test_future_delivered_label_is_not_current(pages):
    assert parse_tracking(pages["tracking"], "1001").status != "Delivered"


@pytest.mark.parametrize(
    "action,expected",
    [
        ("Pending Confirmation", "Pending Confirmation"),
        ("Action Required", "Action Required"),
        ("Contact the pharmacist", "Action Required"),
    ],
)
def test_medication_specific_actions(pages, action, expected):
    html = pages["tracking"].replace("Prescription Required", action)
    assert parse_tracking(html, "1001").medications[0].status == expected


def test_hidden_action_panels_are_ignored(pages):
    html = pages["tracking"].replace(
        'class="prescription-confirmation-items"',
        'class="prescription-confirmation-items" style="display: none"',
    )
    assert not parse_tracking(html, "1001").medications


def test_same_medicine_for_different_patients_has_different_identity():
    assert (
        Medication("Medicine", "Patient A", "1").key != Medication("Medicine", "Patient B", "1").key
    )


@pytest.mark.parametrize("html", ["<html>Access denied</html>", "<html>New layout</html>"])
def test_unexpected_pages_fail_instead_of_empty_success(html):
    with pytest.raises(ParseError):
        parse_orders(html)
    with pytest.raises(ParseError):
        parse_tracking(html, "1001")


def test_rejects_mismatched_order(pages):
    with pytest.raises(ParseError):
        parse_tracking(pages["tracking"], "1002")


def test_login_detection_ignores_navigation(pages):
    assert is_login(pages["login"])
    assert not is_login('<a href="/login">Login</a>' + pages["account"])
