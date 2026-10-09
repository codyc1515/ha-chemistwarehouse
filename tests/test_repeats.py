"""Parse repeat balances, patient groups and expiry dates."""

from datetime import date

import pytest

from custom_components.chemistwarehouse.parser import ParseError, PrescriptionRepeat, parse_repeats


def test_repeats_patient_age_is_removed_for_matching(pages):
    values = parse_repeats(pages["repeats"])
    assert values == (
        PrescriptionRepeat(
            "Example Medicine 10mg 30 tablets", "Test Patient", date(2024, 4, 12), 1
        ),
        PrescriptionRepeat(
            "Example Medicine 10mg 30 tablets", "Second Patient", date(2025, 9, 18), 2
        ),
    )
    assert values[0].key != values[1].key


def test_raw_repeat_table_does_not_need_tbody(pages):
    assert len(parse_repeats(pages["repeats"].replace("<tbody>", "").replace("</tbody>", ""))) == 2


def test_empty_repeats_table_is_valid(pages):
    assert parse_repeats(pages["account"]) == ()


@pytest.mark.parametrize("replacement", ["invalid", "31 Feb 2024"])
def test_invalid_expiry_rejected(pages, replacement):
    with pytest.raises(ParseError):
        parse_repeats(pages["repeats"].replace("12 Apr 2024", replacement))


def test_missing_table_does_not_silently_erase_calendar():
    with pytest.raises(ParseError):
        parse_repeats("<html>Changed account layout</html>")
