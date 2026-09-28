"""Phone normalisation. Runs inside the validation process pool, so keep it import-light and pure."""
import json
import re
from pathlib import Path

import phonenumbers
from phonenumbers import PhoneNumberType

NON_DIGIT = re.compile(r"\D")
FAST_IN = re.compile(r"^(?:91|0)?([6-9]\d{9})$")
SCI = re.compile(r"^\s*[+-]?\d+(?:\.\d+)?[eE][+-]?\d+\s*$")
LANDLINE_TYPES = {PhoneNumberType.FIXED_LINE}
HEADER_HINTS = {"mobile": 1.0, "phone": 0.9, "cell": 0.9, "whatsapp": 0.9, "number": 0.7, "contact": 0.6, "tel": 0.6}
NAME_HINTS = ("full name", "name", "display name", "contact name", "first name")


# Country column values -> region code. Built from libphonenumber's metadata: ISO codes,
# English names and calling codes, plus the spellings people actually type.
ALIASES = {
    "uk": "GB", "u.k.": "GB", "england": "GB", "scotland": "GB", "wales": "GB", "northern ireland": "GB",
    "great britain": "GB", "britain": "GB", "gbr": "GB",
    "usa": "US", "u.s.": "US", "u.s.a.": "US", "america": "US", "united states of america": "US",
    "uae": "AE", "u.a.e.": "AE", "emirates": "AE", "are": "AE",
    "ind": "IN", "bharat": "IN", "aus": "AU", "can": "CA", "sgp": "SG", "deu": "DE", "fra": "FR",
    "ksa": "SA", "sau": "SA", "nzl": "NZ", "zaf": "ZA", "pak": "PK", "bgd": "BD", "npl": "NP", "lka": "LK",
    "korea": "KR", "south korea": "KR", "russia": "RU", "holland": "NL",
}


# English country names per region. Generated once from phonenumbers.geocoder, whose import
# costs ~100 MB of RSS in every worker process just to get these 245 strings.
REGION_NAMES = json.loads((Path(__file__).parent / "country_names.json").read_text(encoding="utf-8"))


def _build_country_lookup():
    table = {}
    for code, regions in phonenumbers.COUNTRY_CODE_TO_REGION_CODE.items():
        main = next((r for r in regions if r != "001"), None)
        if main:
            table.setdefault(str(code), main)
            table.setdefault(f"+{code}", main)
    for region in phonenumbers.SUPPORTED_REGIONS:
        table[region.lower()] = region
        name = REGION_NAMES.get(region)
        if name:
            table[name.lower()] = region
    table.update(ALIASES)
    return table


COUNTRY = _build_country_lookup()


# [(code, English name)] for the default-country picker
REGIONS = sorted(((r, REGION_NAMES.get(r, r)) for r in phonenumbers.SUPPORTED_REGIONS), key=lambda r: r[1])


def region_for(value, default):
    """Map a country cell ('India', 'IN', 'uk', '+44', '1', ...) to a region code, else the default."""
    if not value:
        return default
    return COUNTRY.get(value.strip().lower(), default)


def normalize(raw: str, region: str = "IN", reject_landline: bool = False, infer_cc: bool = True):
    """Return (e164, None) or (None, reason). Reasons: empty | invalid | sci_notation | landline."""
    e164, reason, _ = _normalize(raw, region, reject_landline, infer_cc)
    return e164, reason


def _valid(text, region):
    try:
        num = phonenumbers.parse(text, region)
    except phonenumbers.NumberParseException:
        return None
    return num if phonenumbers.is_valid_number(num) else None


def _normalize(raw, region, reject_landline, infer_cc=True):
    """Same as normalize() plus a flag telling whether the regex fast path answered.

    Order of attempts:
      1. '+' or '00' prefix: the number carries its own country code; parse it as international.
      2. Indian mobile fast path (region IN, or an explicit +91), which skips the slow parser.
      3. Parse as a national number of `region` (the row's country column, else the import default).
      4. With infer_cc, 11+ digits that fail step 3 are retried as if a '+' had been dropped
         (447911123456 -> +44 7911 123456). Shorter numbers are never guessed.
    """
    if raw is None:
        return None, "empty", False
    s = raw.strip()
    if not s:
        return None, "empty", False
    if SCI.match(s):
        return None, "sci_notation", False  # Excel already dropped the trailing digits; unrecoverable
    digits = NON_DIGIT.sub("", s)
    if not digits:
        return None, "invalid", False
    explicit = s.startswith("+") or s.startswith("00")
    if s.startswith("00"):
        digits = digits[2:]

    m = None
    if explicit:
        if len(digits) == 12 and digits.startswith("91"):
            m = FAST_IN.match(digits)
    elif region == "IN":
        m = FAST_IN.match(digits)
    if m:
        # Some STD codes (080, 079, ...) also start with 6-9, so landline rejection still needs a
        # type lookup, but on a prebuilt number, which skips the expensive parse().
        if reject_landline and phonenumbers.number_type(
                phonenumbers.PhoneNumber(country_code=91, national_number=int(m.group(1)))) in LANDLINE_TYPES:
            return None, "landline", True
        return "+91" + m.group(1), None, True

    if explicit:
        num = _valid("+" + digits, None)
    else:
        num = _valid(s, region)
        if num is None and infer_cc and len(digits) >= 11:
            num = _valid("+" + digits, None)
    if num is None:
        return None, "invalid", False
    if reject_landline and phonenumbers.number_type(num) in LANDLINE_TYPES:
        return None, "landline", False
    return phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164), None, False


def validate_batch(rows, var_names, region, reject_landline, infer_cc=True):
    """rows: [(row_no, (phone candidates in priority order), name, (var values), country cell)].

    The country cell, when mapped, picks the region for numbers written without a country code.
    First valid candidate wins. A rejected row reports the first non-empty raw value and its reason.
    """
    ok, bad, reasons, fast = [], [], {}, 0
    for row_no, candidates, name, var_values, country in rows:
        row_region = region_for(country, region)
        e164 = None
        first_raw, first_reason = None, "empty"
        for raw in candidates:
            e164, reason, was_fast = _normalize(raw, row_region, reject_landline, infer_cc)
            if e164:
                fast += was_fast
                break
            if first_raw is None and reason != "empty":
                first_raw, first_reason = raw, reason
        if e164:
            vars_json = json.dumps(dict(zip(var_names, var_values)), ensure_ascii=False) if var_names else None
            ok.append((row_no, e164, (name or "").strip() or None, vars_json))
        else:
            bad.append((row_no, first_raw, first_reason))
            reasons[first_reason] = reasons.get(first_reason, 0) + 1
    return ok, bad, reasons, fast


def score_columns(headers, sample_rows, region="IN", country_idx=None):
    """Score each column as a phone column: header-name match + share of sampled values that are valid.

    With country_idx, each sampled value is checked against its own row's country.
    """
    out = []
    for i, h in enumerate(headers):
        low = h.lower()
        name_score = max((w for k, w in HEADER_HINTS.items() if k in low), default=0.0)
        pairs = [(r[i], region_for(r[country_idx] if country_idx is not None and country_idx < len(r) else "", region))
                 for r in sample_rows if i < len(r) and r[i].strip()]
        values = [v for v, _ in pairs]
        checks = [normalize(v, rg) for v, rg in pairs]
        valid = sum(1 for e164, _ in checks if e164)
        valid_pct = valid / len(values) if values else 0.0
        fill_pct = len(values) / len(sample_rows) if sample_rows else 0.0
        score = 0.4 * name_score + 0.6 * valid_pct
        out.append({"index": i, "header": h, "name_score": round(name_score, 2), "valid_pct": round(valid_pct, 3),
                    "fill_pct": round(fill_pct, 3), "score": round(score, 3),
                    "examples": [[v, e164, reason] for v, (e164, reason) in list(zip(values, checks))[:4]]})
    return out


def suggest_country(headers):
    """A column whose header says country (Outlook exports 'Home Country', 'Business Country')."""
    low = [h.lower().strip() for h in headers]
    for exact in ("country", "country code", "home country", "business country", "country/region"):
        if exact in low:
            return headers[low.index(exact)]
    return next((headers[i] for i, h in enumerate(low) if "country" in h), None)


def suggest_name(headers):
    low = [h.lower().strip() for h in headers]
    for hint in NAME_HINTS:
        for i, h in enumerate(low):
            if h == hint:
                return headers[i]
    for i, h in enumerate(low):
        if "name" in h and "company" not in h and "file" not in h:
            return headers[i]
    return None
