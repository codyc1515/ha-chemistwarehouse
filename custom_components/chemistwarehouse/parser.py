"""Parse the website without making requests or importing Home Assistant."""

import re
from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from .const import BASE_URL, STATUSES


class ParseError(Exception):
    """The website returned an unexpected page."""


def text(node: Tag | None) -> str:
    """Normalize the site's whitespace."""
    return " ".join(node.get_text(" ", strip=True).split()) if node else ""


def safe_url(href: str, base: str = BASE_URL) -> str:
    """Keep credentials and account requests on the expected HTTPS origin."""
    url = urljoin(base, href)
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.netloc != urlsplit(BASE_URL).netloc
        or parts.username
        or parts.password
    ):
        raise ParseError("Unexpected website URL")
    return url


def is_login(html: str) -> bool:
    """Do not mistake the navigation's login link for a login form."""
    return BeautifulSoup(html, "html.parser").select_one("#password1") is not None


def login_form(html: str, username: str, password: str) -> tuple[str, dict]:
    """Replay the ASP.NET form, including fresh viewstate and submit control."""
    soup = BeautifulSoup(html, "html.parser")
    user, secret, submit = (soup.select_one(s) for s in ("#username", "#password1", "#Signin"))
    if user is None or secret is None or submit is None:
        raise ParseError("Login form was not found")
    form = secret.find_parent("form")
    if form is None or any(not node.get("name") for node in (user, secret, submit)):
        raise ParseError("Login form is incomplete")
    # The supplied page places __VIEWSTATE outside the closing form tag.
    fields = {
        node["name"]: node.get("value", "")
        for node in soup.select('input[type="hidden"][name]')
        if not node.has_attr("disabled")
    }
    fields.update(
        {
            user["name"]: username,
            secret["name"]: password,
            submit["name"]: submit.get("value", "SIGN IN"),
        }
    )
    return safe_url(form.get("action", "/login"), BASE_URL + "/login"), fields


@dataclass(frozen=True)
class Order:
    """One prescription order in the account page."""

    order_id: str
    tracking_url: str
    ordered_at: str
    account_status: str
    invoice_url: str = ""

    @property
    def sort_key(self) -> tuple:
        """Sort by the NZ day/month date; order ID breaks ties."""
        try:
            date = datetime.strptime(self.ordered_at, "%d/%m/%Y %I:%M:%S %p")
        except ValueError:
            date = datetime.min
        return date, int(self.order_id) if self.order_id.isdigit() else 0


@dataclass(frozen=True)
class Medication:
    """Identity includes the patient so household prescriptions do not merge."""

    name: str
    patient: str
    quantity: str
    status: str | None = None

    @property
    def key(self) -> str:
        return sha256(f"{self.patient.casefold()}\0{self.name.casefold()}".encode()).hexdigest()


@dataclass(frozen=True)
class Tracking:
    status: str
    medications: tuple[Medication, ...]
    shipment_statuses: tuple[str, ...]
    action_required: bool


@dataclass(frozen=True)
class PrescriptionRepeat:
    """Repeat balance and expiry reported by the online dispensary."""

    name: str
    patient: str
    expires: date
    remaining: int

    @property
    def key(self) -> str:
        return Medication(self.name, self.patient, "").key


def parse_repeats(html: str) -> tuple[PrescriptionRepeat, ...]:
    """Read patient groups and repeat rows without requiring browser tbody tags."""
    soup = BeautifulSoup(html, "html.parser")
    table = next(
        (
            table
            for table in soup.select("table")
            if [text(th).casefold() for th in table.select("thead th")]
            == ["patient / product", "expires", "repeats left"]
        ),
        None,
    )
    if table is None:
        raise ParseError("Prescription repeats table was not found")
    patient = ""
    repeats = []
    for row in table.select("tr"):
        if row.find_parent("table") is not table or row.find("th", recursive=False):
            continue
        cells = row.find_all("td", recursive=False)
        if not cells or not text(row):
            continue
        if len(cells) == 1 and cells[0].get("colspan") == "3":
            patient = re.sub(r"\s*\(\d+\s*yo\)\s*$", "", text(cells[0]), flags=re.I).strip()
            continue
        if len(cells) != 3 or not patient or not text(cells[0]):
            raise ParseError("Prescription repeat row is incomplete")
        try:
            expires = datetime.strptime(text(cells[1]), "%d %b %Y").date()
            count = text(cells[2])
            if not count.isdigit():
                raise ValueError
            remaining = int(count)
        except ValueError as err:
            raise ParseError("Prescription repeat date or balance is invalid") from err
        repeats.append(PrescriptionRepeat(text(cells[0]), patient, expires, remaining))
    return tuple(repeats)


def parse_orders(html: str) -> list[Order]:
    """Only follow prescription tracking links in account order records."""
    soup = BeautifulSoup(html, "html.parser")
    if soup.select_one(".account-order-table__body") is None:
        raise ParseError("Account order list was not found")
    orders = {}
    for row in soup.select(".account-order-table-record"):
        for link in row.select("a[href]"):
            if text(link).casefold() != "track your order":
                continue
            try:
                url = safe_url(link["href"])
            except ParseError:
                continue
            parts = urlsplit(url)
            if parts.path.casefold().rstrip("/") != "/track-your-order":
                continue
            order_id = parse_qs(parts.query).get("o", [""])[0]
            if not order_id:
                raise ParseError("Prescription tracking link has no order ID")
            invoice_url = ""
            for detail in row.select("a[href]"):
                if text(detail).casefold() != "view order":
                    continue
                candidate = safe_url(detail["href"])
                invoice = urlsplit(candidate)
                if (
                    invoice.path.casefold() != "/cmsglobalfiles/pages/invoice.aspx"
                    or parse_qs(invoice.query).get("OrderID", [""])[0] != order_id
                ):
                    raise ParseError("Invoice link does not match the prescription order")
                invoice_url = candidate
            orders[order_id] = Order(
                order_id,
                url,
                text(row.select_one(".account-order-table__item--date")),
                text(row.select_one(".account-order-table__item--status")),
                invoice_url,
            )
    return sorted(orders.values(), key=lambda order: order.sort_key, reverse=True)


def parse_invoice(html: str, order_id: str) -> tuple[Medication, ...]:
    """Match ordered products to patient mappings, excluding profile history.

    The live invoice's patient tables are server-rendered inside a hidden div.
    Only their Name/Product fields identify ordered medications; other fields
    describe medical history and must never create medication events.
    """
    soup = BeautifulSoup(html, "html.parser")
    if not any(text(node) == f"Order #{order_id}" for node in soup.select(".strong")):
        raise ParseError("Invoice does not match the requested order")
    table = soup.select_one("table.productsList")
    if table is None:
        raise ParseError("Invoice inventory was not found")
    header = table.select_one("thead tr")
    # The blank addcol is a layout detail and may be inserted/removed by the
    # website's browser scripts independently in the header and product rows.
    labels = [text(node).casefold() for node in header.select("th") if text(node)] if header else []
    if "product" not in labels or "qty" not in labels:
        raise ParseError("Invoice inventory columns were not found")
    name_index, quantity_index = labels.index("product"), labels.index("qty")
    products = {}
    # Raw server HTML does not necessarily contain the tbody elements a browser
    # inserts. Select this table's own rows and ignore headings and separators.
    for row in table.select("tr"):
        if row.find_parent("table") is not table or row.find("th", recursive=False):
            continue
        if row.find_parent("thead") is not None or row.find_parent("tfoot") is not None:
            continue
        cells = row.find_all("td", recursive=False)
        if cells and not any(text(cell) for cell in cells):
            continue
        cells = [
            cell for cell in cells if not ("addcol" in cell.get("class", []) and not text(cell))
        ]
        if len(cells) != len(labels):
            raise ParseError(
                f"Invoice inventory row is incomplete (expected {len(labels)} columns, "
                f"found {len(cells)})"
            )
        name, quantity = text(cells[name_index]), text(cells[quantity_index])
        if not name or not quantity.isdigit():
            raise ParseError("Invoice product or quantity is invalid")
        normalized = name.casefold()
        if normalized in products:
            old_name, old_quantity = products[normalized]
            products[normalized] = (old_name, str(int(old_quantity) + int(quantity)))
        else:
            products[normalized] = (name, quantity)
    mappings = set()
    for patient_table in soup.select("table.patients__table"):
        for row in patient_table.select("tr"):
            values = {}
            for cell in row.find_all("td", recursive=False):
                label = cell.select_one("strong")
                if label is None:
                    continue
                field = text(label).casefold().rstrip(":")
                if field in ("name", "product"):
                    values[field] = text(cell)[len(text(label)) :].strip()
            if "name" in values and "product" in values:
                name = values["product"].casefold()
                if not values["name"] or name not in products:
                    raise ParseError("Invoice patient product is missing from inventory")
                mappings.add((values["name"], name))
    return tuple(
        Medication(
            products[name][0],
            patient,
            # The inventory quantity is shared when multiple patients order
            # the same product. Do not assign the total to each patient.
            products[name][1] if sum(product == name for _, product in mappings) == 1 else "",
        )
        for patient, name in sorted(mappings)
    )


def normalize_status(value: str) -> str | None:
    """Translate website labels into the requested prescription statuses."""
    normalized = " ".join(value.split()).casefold()
    aliases = {status.casefold(): status for status in STATUSES}
    aliases.update(
        {
            "order confirmation": "Pending Confirmation",
            "pending order confirmation": "Pending Confirmation",
        }
    )
    return aliases.get(normalized)


def visible(node: Tag) -> bool:
    """Ignore server-rendered hidden action panels and their descendants."""
    for parent in (node, *node.parents):
        if not isinstance(parent, Tag):
            continue
        style = str(parent.get("style", "")).replace(" ", "").casefold()
        if parent.has_attr("hidden") or "display:none" in style or "visibility:hidden" in style:
            return False
    return True


def parse_tracking(html: str, order_id: str) -> Tracking:
    """Read active/completed progress steps, never future grey labels."""
    soup = BeautifulSoup(html, "html.parser")
    shipments = []
    for result in soup.select(".repeating_order_result"):
        heading = text(result.select_one("h2"))
        if heading != f"Order No. {order_id}":
            raise ParseError("Tracking page does not match the requested order")
        steps = []
        for step in result.select(".track-progress li"):
            label = step.select_one(".status_red, .status_green")
            if label is not None and visible(label):
                status = normalize_status(text(label))
                if status is None:
                    raise ParseError("Unrecognized tracking progress label")
                steps.append(status)
        if not steps:
            raise ParseError("Tracking progress was not found")
        shipments.append(steps[-1])
    if not shipments:
        raise ParseError("Tracking order was not found")
    # A split shipment is delivered only when every shipment is delivered.
    status = min(shipments, key=STATUSES.index)
    banner = soup.select_one(".prescription-tracking__action-banner")
    action_required = banner is not None and visible(banner)
    medications = []
    prefix = ".prescription-confirmation-items__"
    for group in soup.select(prefix + "patient-group"):
        if not visible(group):
            continue
        patient_node = group.select_one(prefix + "patient-name")
        patient = text(patient_node.select_one("span:last-child")) if patient_node else ""
        for item in group.select(prefix + "item-grid-item"):
            if not visible(item):
                continue
            name = text(item.select_one(prefix + "item-grid-item-name"))
            if not name:
                raise ParseError("Medication has no name")
            action = text(item.select_one(prefix + "item-grid-item-action"))
            item_status = normalize_status(action)
            if action and item_status is None:
                item_status = "Action Required"
            medications.append(
                Medication(
                    name,
                    patient,
                    text(item.select_one(prefix + "item-grid-item-quantity")),
                    item_status,
                )
            )
    return Tracking(status, tuple(medications), tuple(shipments), action_required)
