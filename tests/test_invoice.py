"""Verify invoice discovery without importing medical history as orders."""

import pytest
from bs4 import BeautifulSoup

from custom_components.chemistwarehouse.parser import (
    Medication,
    ParseError,
    parse_invoice,
    parse_orders,
)


def test_invoice_identifies_only_ordered_patient_products(pages):
    assert parse_invoice(pages["invoice"], "1001") == (
        Medication("Example Medicine 10mg 30 tablets", "Test Patient", "1"),
    )


def test_hidden_patient_mapping_is_used_but_history_is_excluded(pages):
    meds = parse_invoice(pages["invoice"], "1001")
    assert meds[0].patient == "Test Patient"
    assert all("history" not in med.name for med in meds)


def test_same_product_for_multiple_patients_does_not_assign_total_twice(pages):
    soup = BeautifulSoup(pages["invoice"], "html.parser")
    table = soup.select_one("table.patients__table")
    other = BeautifulSoup(str(table).replace("Test Patient", "Second Patient"), "html.parser")
    table.insert_after(other)
    meds = parse_invoice(str(soup), "1001")
    assert len(meds) == 2
    assert meds[0].key != meds[1].key
    assert all(med.quantity == "" for med in meds)


def test_invoice_header_columns_allow_hidden_addcol_to_be_removed(pages):
    soup = BeautifulSoup(pages["invoice"], "html.parser")
    for node in soup.select(".addcol"):
        node.decompose()
    assert parse_invoice(str(soup), "1001")[0].quantity == "1"


def test_invoice_body_spacer_row_does_not_count_as_a_product(pages):
    soup = BeautifulSoup(pages["invoice"], "html.parser")
    spacer = soup.select_one("td.separator-space").parent.extract()
    soup.select_one("table.productsList tbody").insert(0, spacer)
    assert parse_invoice(str(soup), "1001")[0].quantity == "1"


@pytest.mark.parametrize("selector", ["thead .addcol", "tbody .addcol"])
def test_invoice_optional_layout_column_can_differ_between_header_and_body(pages, selector):
    soup = BeautifulSoup(pages["invoice"], "html.parser")
    for node in soup.select(selector):
        node.decompose()
    assert parse_invoice(str(soup), "1001")[0].quantity == "1"


def test_raw_invoice_rows_do_not_require_browser_inserted_tbody(pages):
    soup = BeautifulSoup(pages["invoice"], "html.parser")
    soup.select_one("table.productsList tbody").unwrap()
    assert parse_invoice(str(soup), "1001")[0].quantity == "1"


def test_incomplete_product_row_is_still_rejected(pages):
    soup = BeautifulSoup(pages["invoice"], "html.parser")
    soup.select_one("table.productsList tbody tr").find_all("td")[4].decompose()
    with pytest.raises(ParseError):
        parse_invoice(str(soup), "1001")


@pytest.mark.parametrize(
    "html",
    [
        '<div class="strong">Order #1002</div>',
        '<div class="strong">Order #1001</div>',
        '<div class="strong">Order #1001</div><table class="productsList"></table>',
        "<html>Access denied</html>",
    ],
)
def test_unexpected_invoice_fails_instead_of_guessing(html):
    with pytest.raises(ParseError):
        parse_invoice(html, "1001")


def test_patient_product_must_be_present_in_order_inventory(pages):
    html = pages["invoice"].replace(
        "<strong>Product:</strong> Example Medicine 10mg 30 tablets",
        "<strong>Product:</strong> Unordered medicine",
    )
    with pytest.raises(ParseError):
        parse_invoice(html, "1001")


def test_prescription_invoice_link_is_attached_to_its_own_order(pages):
    html = pages["account"].replace(
        '<a href="/track-your-order',
        '<a href="/cmsglobalfiles/pages/invoice.aspx?OrderID=1001&amp;ID=test">View Order</a>'
        '<a href="/track-your-order',
    )
    order = parse_orders(html)[0]
    assert order.invoice_url.endswith("invoice.aspx?OrderID=1001&ID=test")
    with pytest.raises(ParseError):
        parse_orders(html.replace("OrderID=1001", "OrderID=1002"))
