import pytest

from app.phones import normalize, region_for, validate_batch


@pytest.mark.parametrize("raw, region, expected", [
    # Indian mobiles, fast path
    ("+91 98765 43210", "IN", "+919876543210"),
    ("098765 43210", "IN", "+919876543210"),
    ("919876543210", "IN", "+919876543210"),
    ("+91 98765 43210", "US", "+919876543210"),       # explicit code wins over the default
    # Numbers that carry their own country code
    ("+44 7911 123456", "IN", "+447911123456"),
    ("0044 7911 123456", "US", "+447911123456"),       # 00 prefix treated as international
    ("011 44 7911 123456", "US", "+447911123456"),     # US international dialling prefix
    ("+971 50 123 4567", "IN", "+971501234567"),
    # National formats, read with the row's country
    ("(415) 555-0100", "US", "+14155550100"),
    ("07911 123456", "GB", "+447911123456"),
    ("050 123 4567", "AE", "+971501234567"),
    ("8123 4567", "SG", "+6581234567"),
    # Missing '+': 11+ digits retried as international
    ("447911123456", "IN", "+447911123456"),
    ("14155550100", "IN", "+14155550100"),
])
def test_normalize_valid(raw, region, expected):
    assert normalize(raw, region) == (expected, None)


@pytest.mark.parametrize("raw, reason", [
    ("", "empty"),
    ("   ", "empty"),
    ("9.19877E+11", "sci_notation"),
    ("12345", "invalid"),
    ("n/a", "invalid"),
])
def test_normalize_rejects(raw, reason):
    assert normalize(raw, "IN") == (None, reason)


def test_missing_plus_guess_can_be_disabled():
    assert normalize("447911123456", "IN", infer_cc=False) == (None, "invalid")


def test_landline_rule():
    assert normalize("080 2345 6789", "IN") == ("+918023456789", None)
    assert normalize("080 2345 6789", "IN", reject_landline=True) == (None, "landline")


@pytest.mark.parametrize("value, expected", [
    ("India", "IN"), ("IN", "IN"), ("united kingdom", "GB"), ("UK", "GB"), ("England", "GB"),
    ("USA", "US"), ("United States", "US"), ("+44", "GB"), ("1", "US"), ("UAE", "AE"),
    ("", "IN"), ("Narnia", "IN"),
])
def test_region_for(value, expected):
    assert region_for(value, "IN") == expected


def test_country_column_decides_ambiguous_numbers():
    # Local UK/US formats are also valid Indian mobiles. Without the row's country they are silently
    # turned into a different, real Indian number, so only the country column can tell them apart.
    rows = [(1, ("07775 559513",), "a", (), "United Kingdom"),
            (2, ("07775 559513",), "b", (), ""),
            (3, ("(717) 751-1028",), "c", (), "USA"),
            (4, ("(717) 751-1028",), "d", (), ""),
            (5, ("", "050 123 4567"), "e", (), "UAE")]
    ok, bad, reasons, _ = validate_batch(rows, [], "IN", False)
    assert [r[1] for r in ok] == ["+447775559513", "+917775559513", "+17177511028", "+917177511028",
                                  "+971501234567"]
    assert bad == [] and reasons == {}
