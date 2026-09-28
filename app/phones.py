"""Phone validation. Runs inside the worker's process pool, so keep it import-light and pure."""
import json
import re
from pathlib import Path

import phonenumbers
from phonenumbers import PhoneNumberFormat

NON_DIGIT = re.compile(r"\D")
FAST_IN = re.compile(r"^(?:91|0)?([6-9]\d{9})$")          # Indian mobile, the common case
SCI = re.compile(r"^\s*[+-]?\d+(?:\.\d+)?[eE][+-]?\d+\s*$")  # Excel's 9.19E+11: digits already lost

# Country cell -> region code: ISO codes, English names, calling codes and common spellings.
# Names ship as JSON because importing phonenumbers.geocoder costs ~100 MB RSS per process.
REGION_NAMES = json.loads((Path(__file__).parent / "country_names.json").read_text(encoding="utf-8"))
ALIASES = {
    "uk": "GB", "england": "GB", "scotland": "GB", "wales": "GB", "great britain": "GB", "britain": "GB",
    "usa": "US", "america": "US", "united states of america": "US", "uae": "AE", "emirates": "AE",
    "ind": "IN", "bharat": "IN", "ksa": "SA", "korea": "KR", "south korea": "KR", "russia": "RU",
}


def _country_lookup():
    table = {}
    for code, regions in phonenumbers.COUNTRY_CODE_TO_REGION_CODE.items():
        main = next((r for r in regions if r != "001"), None)
        if main:
            table.setdefault(str(code), main)
            table.setdefault(f"+{code}", main)
    for region in phonenumbers.SUPPORTED_REGIONS:
        table[region.lower()] = region
        if region in REGION_NAMES:
            table[REGION_NAMES[region].lower()] = region
    table.update(ALIASES)
    return table


COUNTRY = _country_lookup()


def region_for(value, default):
    """'India', 'IN', 'uk', '+44', '1' ... -> region code; anything unknown -> default."""
    return COUNTRY.get(value.strip().lower(), default) if value else default


def _valid(text, region):
    try:
        num = phonenumbers.parse(text, region)
    except phonenumbers.NumberParseException:
        return None
    return num if phonenumbers.is_valid_number(num) else None


def normalize(raw, region="IN"):
    """Return (e164, None) or (None, reason) with reason in empty | invalid | sci_notation.

    1. '+' or '00' prefix: the number carries its own country code.
    2. Indian mobiles take a regex fast path (no slow parser).
    3. Otherwise parse as a national number of `region` (the row's country, else the default).
    4. 11+ digits that fail are retried as if the '+' was dropped (447911123456 -> +447911123456).
    """
    s = (raw or "").strip()
    if not s:
        return None, "empty"
    if SCI.match(s):
        return None, "sci_notation"
    digits = NON_DIGIT.sub("", s)
    if not digits:
        return None, "invalid"
    explicit = s.startswith("+") or s.startswith("00")
    if s.startswith("00"):
        digits = digits[2:]
    if (explicit and len(digits) == 12 and digits.startswith("91")) or (not explicit and region == "IN"):
        m = FAST_IN.match(digits)
        if m:
            return "+91" + m.group(1), None
    num = _valid("+" + digits, None) if explicit else _valid(s, region)
    if num is None and not explicit and len(digits) >= 11:
        num = _valid("+" + digits, None)
    if num is None:
        return None, "invalid"
    return phonenumbers.format_number(num, PhoneNumberFormat.E164), None


def validate_batch(rows, region):
    """rows: [(row_no, phone, name, country, vars_json)] -> (valid rows, rejected rows)."""
    ok, bad = [], []
    for row_no, phone, name, country, vars_json in rows:
        e164, reason = normalize(phone, region_for(country, region))
        if e164:
            ok.append((row_no, e164, name or None, vars_json))
        else:
            bad.append((row_no, phone or None, reason))
    return ok, bad
