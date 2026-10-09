# Chemist Warehouse prescriptions for Home Assistant

Custom integration for **Chemist Warehouse New Zealand**. Sign in through Home
Assistant's UI to view prescription order statuses, expiry dates and estimated repeat reminders
in a single calendar.

**This integration only works with prescriptions sent to Chemist Warehouse Online
New Zealand**, the online dispensary. It does not track prescriptions sent to or
held by local Chemist Warehouse stores, which operate separately from the online
dispensary.

This is an independent, unofficial community integration. It is not affiliated
with, supported by, endorsed by, or sponsored by Chemist Warehouse. For integration
issues, use this repository's issue tracker.

## Install

1. Copy `custom_components/chemistwarehouse` into your Home Assistant
   `config/custom_components/` directory.
2. Restart Home Assistant.
3. Open **Settings → Devices & services → Add integration**, search for
   **Chemist Warehouse**, and enter your website username/email and password.

Home Assistant 2025.12 or newer is required. Alternatively, once this repository
is published, add `https://github.com/codyc1515/ha-chemistwarehouse` to HACS as a
custom **Integration** repository, install it, and restart Home Assistant.

## Order information

One calendar is created per account. Each patient and medication follows its newest
known prescription order. An all-day event on that order's date shows the medication
name and current status. Patient names are not appended to event titles. Statuses
are updated in place as the order progresses:

| State | Source |
| --- | --- |
| Action Required | An item requires attention without a more specific recognized status, or a general action banner with no item list |
| Prescription Required | The medication's prescription requirement |
| Pending Confirmation | Order confirmation progress |
| Processing order | Processing progress |
| Awaiting Shipment | Shipment preparation progress |
| In Transit | Transit progress |
| Delivered | All shipments for the order have reached delivered |

Medication requirements take precedence over order progress. Items that no longer
require attention follow the order progress bar. Split shipments use the least
advanced shipment's status because the supplied markup does not assign medications
to individual shipments. Event descriptions list each shipment's status, the order
number, order date/time and quantity (ordered quantity, not remaining doses).
Matching prescriptions also include the remaining repeat count and expiry date.
If the same product is ordered for multiple patients and the invoice only supplies
a combined quantity, `quantity` is left empty unless tracking gives an individual
patient quantity.

The integration checks the account every **6 hours**, fetching each prescription
tracking page and associated invoice once per update. It accepts same-origin `/track-your-order` links
and excludes `/c/tracking` retail orders. New medications appear automatically.
The calendar becomes unavailable on failed updates. Orders no longer visible in
the account are removed from the calendar. Login expiry triggers one automatic
login retry; rejected credentials prompt you to reauthenticate.

## Repeat reminders and expiry

Each account has a read-only **Chemist Warehouse prescriptions** calendar. It shows
the expiry dates and remaining repeat counts from the account's repeats table.
Repeat reminders are **estimates**, not pharmacy-confirmed dispensing or eligibility
dates.

For a matching patient and medication, the integration divides the period between
the newest known order date and prescription expiry into `repeats remaining + 1`
equal parts. One remaining repeat gets a reminder halfway through that period;
two get reminders at one-third and two-thirds. Dates are rounded down to whole
calendar days and shown as all-day events in Home Assistant's configured timezone.
Estimates recalculate when the order date, repeat count or expiry changes during
the six-hour refresh.

If there is no matching order, its date cannot be parsed, or expiry is on or before
the order date, no repeat estimates are created; the expiry event is still shown.
Zero remaining repeats also produces no repeat reminders. Order events remain
independent of repeat data. Reminder dates must fall strictly between the order
date and expiry. Calendar titles contain the medication name without the patient's
name.

The calendar entity's `medications` attribute contains the current medication
names, statuses and order details, including any matching repeat and expiry data.
This remains accessible after the order-date events have passed. Earlier versions'
medication sensors are removed automatically when this version loads.

## Medication discovery and verification

Medication names are discovered from the prescription's **View Order** invoice,
by matching the ordered product inventory with its patient-to-product tables.
Those patient tables are present in the invoice HTML even when hidden in the
browser. Ordinary retail items and medications mentioned only in the patient's
medical history do not create medication events. The tracking page's **Items Requiring
Attention** list supplies medication-specific requirements and quantities.

Invoice discovery allows a first installation to identify medications after they
have left the attention list, provided the invoice retains the observed structure.
Discovered names are also remembered locally across restarts. If neither page
identifies medications, the integration logs a warning and does not invent medication events.

The account, invoice, repeats table and tracking pages were inspected in a live
logged-in browser. Pending confirmation, prescription-required and in-transit
tracking markup have been observed. Other progress states are supported using the
same red/green progress classes and covered with synthetic fixtures.
The integration's automated HTTP login still needs
end-to-end verification in Home Assistant; browser sign-in was performed by the user.

## Local data

Credentials are stored in Home Assistant's config entry. Medication names, patient
names, quantities and order IDs are cached under Home Assistant's `.storage`
directory; removing the integration deletes this cache. Session cookies stay in
memory. Raw pages, credentials,
and private tracking URLs are not logged or exposed in calendar data. The test
fixtures contain sanitized excerpts rather than your complete account pages.

The integration reads account, invoice and tracking pages; it does not submit prescriptions
or change orders. CAPTCHA or other browser challenges are reported as connection
or unexpected-page errors.

## Development

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
.venv/bin/ruff check custom_components tests
.venv/bin/ruff format --check custom_components tests
```

The implementation follows Home Assistant's
[config flow](https://developers.home-assistant.io/docs/core/integration/config_flow/)
and [coordinated polling](https://developers.home-assistant.io/docs/integration_fetching_data/)
patterns. Tests exercise the parser, HTTP/session behavior, coordinator persistence,
reauthentication, order events and repeat calendars using Home Assistant classes without making
requests to a real customer account.
