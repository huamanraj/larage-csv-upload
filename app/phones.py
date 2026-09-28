"""Phone normalisation. Runs inside the validation process pool, so keep it import-light and pure."""
import json
import re

import phonenumbers
from phonenumbers import PhoneNumberType

NON_DIGIT = re.compile(r"\D")
FAST_IN = re.compile(r"^(?:91|0)?([6-9]\d{9})$")
SCI = re.compile(r"^\s*[+-]?\d+(?:\.\d+)?[eE][+-]?\d+\s*$")
LANDLINE_TYPES = {PhoneNumberType.FIXED_LINE}
HEADER_HINTS = {"mobile": 1.0, "phone": 0.9, "cell": 0.9, "whatsapp": 0.9, "number": 0.7, "contact": 0.6, "tel": 0.6}
NAME_HINTS = ("full name", "name", "display name", "contact name", "first name")


def normalize(raw: str, region: str = "IN", reject_landline: bool = False):
    """Return (e164, None) or (None, reason). Reasons: empty | invalid | sci_notation | landline."""
    e164, reason, _ = _normalize(raw, region, reject_landline)
    return e164, reason


def _normalize(raw, region, reject_landline):
    """Same as normalize() plus a flag telling whether the regex fast path answered."""
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
    if region == "IN" and not s.startswith("00"):
        m = FAST_IN.match(digits)
        if m:
            # Some STD codes (080, 079, ...) also start with 6-9, so landline rejection still needs a
            # type lookup, but on a prebuilt number, which skips the expensive parse().
            if reject_landline and phonenumbers.number_type(
                    phonenumbers.PhoneNumber(country_code=91, national_number=int(m.group(1)))) in LANDLINE_TYPES:
                return None, "landline", True
            return "+91" + m.group(1), None, True
    try:
        num = phonenumbers.parse(s, region)
    except phonenumbers.NumberParseException:
        return None, "invalid", False
    if not phonenumbers.is_valid_number(num):
        return None, "invalid", False
    if reject_landline and phonenumbers.number_type(num) in LANDLINE_TYPES:
        return None, "landline", False
    return phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164), None, False


def validate_batch(rows, var_names, region, reject_landline):
    """rows: [(row_no, (phone candidates in priority order), name, (var values))].

    First valid candidate wins. A rejected row reports the first non-empty raw value and its reason.
    """
    ok, bad, reasons, fast = [], [], {}, 0
    for row_no, candidates, name, var_values in rows:
        e164 = None
        first_raw, first_reason = None, "empty"
        for raw in candidates:
            e164, reason, was_fast = _normalize(raw, region, reject_landline)
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


def score_columns(headers, sample_rows, region="IN"):
    """Score each column as a phone column: header-name match + share of sampled values that are valid."""
    out = []
    for i, h in enumerate(headers):
        low = h.lower()
        name_score = max((w for k, w in HEADER_HINTS.items() if k in low), default=0.0)
        values = [r[i] for r in sample_rows if i < len(r) and r[i].strip()]
        checks = [normalize(v, region) for v in values]
        valid = sum(1 for e164, _ in checks if e164)
        valid_pct = valid / len(values) if values else 0.0
        fill_pct = len(values) / len(sample_rows) if sample_rows else 0.0
        score = 0.4 * name_score + 0.6 * valid_pct
        out.append({"index": i, "header": h, "name_score": round(name_score, 2), "valid_pct": round(valid_pct, 3),
                    "fill_pct": round(fill_pct, 3), "score": round(score, 3),
                    "examples": [[v, e164, reason] for v, (e164, reason) in list(zip(values, checks))[:4]]})
    return out


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
