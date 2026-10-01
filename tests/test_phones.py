import pytest

from app.phones import clean_date, normalize, region_for, validate_batch


@pytest.mark.parametrize("raw, region, expected", [
    ("+91 98765 43210", "IN", "+919876543210"),       # Indian mobile, fast path
    ("098765 43210", "IN", "+919876543210"),
    ("919876543210", "IN", "+919876543210"),
    ("+91 98765 43210", "US", "+919876543210"),       # explicit code wins over the default
    ("+44 7911 123456", "IN", "+447911123456"),       # carries its own country code
    ("0044 7911 123456", "US", "+447911123456"),      # 00 prefix = international
    ("+971 50 123 4567", "IN", "+971501234567"),
    ("(415) 555-0100", "US", "+14155550100"),         # national formats, row's country
    ("07911 123456", "GB", "+447911123456"),
    ("050 123 4567", "AE", "+971501234567"),
    ("447911123456", "IN", "+447911123456"),          # '+' dropped, 11+ digits retried
    ("14155550100", "IN", "+14155550100"),
])
def test_valid(raw, region, expected):
    assert normalize(raw, region) == (expected, None)


@pytest.mark.parametrize("raw, reason", [
    ("", "empty"), ("   ", "empty"), (None, "empty"),
    ("9.19877E+11", "sci_notation"), ("12345", "invalid"), ("n/a", "invalid"),
    ("12", "too_short"), ("98-76", "too_short"),
    ("9999999999", "junk"), ("+91 90000 00000", "junk"), ("+1 412 222 2222", "junk"),
])
def test_rejects(raw, reason):
    assert normalize(raw, "IN") == (None, reason)


@pytest.mark.parametrize("value, expected", [
    ("India", "IN"), ("IN", "IN"), ("united kingdom", "GB"), ("UK", "GB"), ("USA", "US"),
    ("+44", "GB"), ("1", "US"), ("UAE", "AE"), ("", "IN"), ("Narnia", "IN"),
])
def test_region_for(value, expected):
    assert region_for(value, "IN") == expected


def test_country_decides_local_numbers():
    # A local UK number is also a valid Indian mobile: only the row's country tells them apart.
    rows = [(1, ("07775 559513",), "a", "United Kingdom", ()),
            (2, ("07775 559513",), "b", "", ()),
            (3, ("12",), "c", "", ())]
    ok, bad, stats = validate_batch(rows, "IN")
    assert ok == [(1, "+447775559513", "a", None), (2, "+917775559513", "b", None)]
    assert bad == [(3, "12", "too_short")]
    assert stats["reasons"] == {"too_short": 1}


def test_first_valid_phone_column_wins():
    rows = [(1, ("", "555-555-1212", "+1 425 882 8080"), "a", "", ()),   # fake 555 number skipped
            (2, ("", "", ""), "b", "", ()),
            (3, ("n/a", "9.19E+11"), "c", "", ())]
    ok, bad, _ = validate_batch(rows, "IN")
    assert ok == [(1, "+14258828080", "a", None)]
    assert bad == [(2, None, "empty"), (3, "n/a", "invalid")]


def test_email_and_date_fields_are_checked_not_the_row():
    fields = (("City", None), ("E-mail Address", "email"), ("Birthday", "date"))
    rows = [(1, ("98765 43210",), "a", "", ("Pune", "Asha@Example.com", "1990-02-14")),
            (2, ("98765 43211",), "b", "", ("", "not-an-email", "0/0/00")),      # Outlook's empty birthday
            (3, ("98765 43212",), "c", "", ("Delhi", "", "31/02/1990"))]          # 31 Feb does not exist
    ok, bad, stats = validate_batch(rows, "IN", fields)
    assert bad == []
    assert ok == [(1, "+919876543210", "a", '{"City": "Pune", "E-mail Address": "asha@example.com", "Birthday": "1990-02-14"}'),
                  (2, "+919876543211", "b", None),
                  (3, "+919876543212", "c", '{"City": "Delhi"}')]
    assert stats == {"reasons": {}, "email_blanked": 1, "date_blanked": 2}


@pytest.mark.parametrize("value, expected", [
    ("1990-02-14", "1990-02-14"), ("14/02/1990", "1990-02-14"), ("2/14/1990", "1990-02-14"),
    ("14.02.1990", "1990-02-14"), ("0/0/00", ""), ("hello", ""), ("2999-01-01", ""), ("1850-01-01", ""),
])
def test_clean_date(value, expected):
    assert clean_date(value) == expected
