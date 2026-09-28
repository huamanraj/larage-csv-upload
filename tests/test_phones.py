import pytest

from app.phones import normalize, region_for, validate_batch


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
    rows = [(1, "07775 559513", "a", "United Kingdom", None),
            (2, "07775 559513", "b", "", None),
            (3, "12", "c", "", '{"city": "Pune"}')]
    ok, bad = validate_batch(rows, "IN")
    assert ok == [(1, "+447775559513", "a", None), (2, "+917775559513", "b", None)]
    assert bad == [(3, "12", "invalid")]
