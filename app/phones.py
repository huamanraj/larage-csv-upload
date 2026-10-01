"""Row validation: phone numbers (plus e-mail and date fields). Runs inside the worker's process pool, so keep
it import-light and pure."""
import json
import re
from datetime import date, datetime
from pathlib import Path

import phonenumbers
from phonenumbers import PhoneNumberFormat

NON_DIGIT = re.compile(r"\D")
FAST_IN = re.compile(r"^(?:91|0)?([6-9]\d{9})$")          # Indian mobile, the common case
SCI = re.compile(r"^\s*[+-]?\d+(?:\.\d+)?[eE][+-]?\d+\s*$")  # Excel's 9.19E+11: digits already lost
JUNK = re.compile(r"(\d)\1{7}$")                           # 9999999999, 9000000000: placeholders, not people
MIN_DIGITS = 5                                              # shorter is never a phone number
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%d.%m.%Y", "%m/%d/%y", "%d/%m/%y")

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
    """Return (e164, None) or (None, reason), cheapest checks first.

    Reasons: empty | sci_notation (Excel 9.19E+11) | too_short (< 5 digits) | invalid | junk (9999999999).
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
    if len(digits) < MIN_DIGITS:
        return None, "too_short"
    explicit = s.startswith("+") or s.startswith("00")
    if s.startswith("00"):
        digits = digits[2:]
    if (explicit and len(digits) == 12 and digits.startswith("91")) or (not explicit and region == "IN"):
        m = FAST_IN.match(digits)
        if m:
            return (None, "junk") if JUNK.search(m.group(1)) else ("+91" + m.group(1), None)
    num = _valid("+" + digits, None) if explicit else _valid(s, region)
    if num is None and not explicit and len(digits) >= 11:
        num = _valid("+" + digits, None)
    if num is None:
        return None, "invalid"
    if JUNK.search(str(num.national_number)):
        return None, "junk"
    return phonenumbers.format_number(num, PhoneNumberFormat.E164), None


def clean_email(value):
    """Lower-cased address, or '' when it is not an e-mail address (the field is blanked, the row kept)."""
    v = value.strip().lower()
    return v if EMAIL.match(v) else ""


def clean_date(value, today=None):
    """ISO date (YYYY-MM-DD), or '' for placeholders like Outlook's 0/0/00 and impossible dates."""
    v = value.strip()[:10]
    for fmt in DATE_FORMATS:
        try:
            d = datetime.strptime(v, fmt).date()
        except ValueError:
            continue
        return d.isoformat() if date(1900, 1, 1) <= d <= (today or date.today()) else ""
    return ""


CLEANERS = {"email": clean_email, "date": clean_date}


def validate_batch(rows, region, fields=()):
    """rows: [(row_no, (phone candidates in priority order), name, country, (extra cell values))].
    fields: [(column name, kind)] for the extra cells; kind 'email' or 'date' is checked, None is kept as is.

    The first valid phone candidate wins; a rejected row reports its first non-empty value and why.
    Bad e-mails and dates blank that field only. Returns (ok, bad, stats):
      ok    [(row_no, e164, name, vars_json)]
      bad   [(row_no, raw_phone, reason)]
      stats {'reasons': {reason: n}, 'email_blanked': n, 'date_blanked': n}
    """
    ok, bad = [], []
    reasons, blanked = {}, {"email": 0, "date": 0}
    for row_no, phones, name, country, extras in rows:
        row_region = region_for(country, region)
        e164, first = None, None
        for raw in phones:
            e164, reason = normalize(raw, row_region)
            if e164:
                break
            if first is None and reason != "empty":
                first = (raw, reason)
        if not e164:
            first = first or (None, "empty")
            reasons[first[1]] = reasons.get(first[1], 0) + 1
            bad.append((row_no, *first))
            continue
        out = {}
        for (col, kind), v in zip(fields, extras or ()):
            if v and kind:
                c = CLEANERS[kind](v)
                if not c:
                    blanked[kind] += 1
                v = c
            if v:
                out[col] = v
        ok.append((row_no, e164, name or None, json.dumps(out) if out else None))
    return ok, bad, {"reasons": reasons, "email_blanked": blanked["email"], "date_blanked": blanked["date"]}
