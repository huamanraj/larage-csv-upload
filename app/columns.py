"""Which CSV columns hold the phone, name and country.

Two layouts are accepted without any mapping step:
  * the simple schema:   phone, name, country (+ any other columns)
  * an Outlook export:   Mobile Phone / Number / Primary Phone / Business Phone ..., First/Last Name,
                         Home/Business Country/Region (+ ~90 other columns)
"""

# Phone columns in priority order: the first one with a valid number wins.
PHONE_COLUMNS = [
    "phone", "mobile", "mobile phone", "cell", "cell phone", "number", "phone number", "primary phone",
    "business phone", "home phone", "business phone 2", "home phone 2", "company main phone", "other phone",
    "car phone", "callback", "assistant's phone", "radio phone",
]
NOT_PHONE = ("fax", "pager", "telex", "tty", "isdn", "id number")
NAME_COLUMNS = ["name", "full name", "display name", "contact name"]
NAME_PARTS = ["first name", "middle name", "last name"]
COUNTRY_COLUMNS = ["country", "country/region", "home country/region", "business country/region",
                   "other country/region", "home country", "business country"]


def resolve(header):
    """header -> column indices: {'phones': [...], 'name': [...], 'country': [...], 'extra': [(name, i)]}.

    'phones' is empty when the file has no usable phone column.
    """
    low = [h.strip().lower() for h in header]
    pos = {}
    for i, h in enumerate(low):
        pos.setdefault(h, i)

    phones = [pos[c] for c in PHONE_COLUMNS if c in pos]
    # Any other "... phone" / "... mobile" column, after the known ones.
    phones += [i for i, h in enumerate(low)
               if i not in phones and ("phone" in h or "mobile" in h) and not any(x in h for x in NOT_PHONE)]

    full = next((pos[c] for c in NAME_COLUMNS if c in pos), None)
    name = [full] if full is not None else [pos[c] for c in NAME_PARTS if c in pos]
    if not name and "e-mail display name" in pos:
        name = [pos["e-mail display name"]]

    country = [pos[c] for c in COUNTRY_COLUMNS if c in pos]
    used = set(phones) | set(name) | set(country)
    extra = [(header[i].strip(), i) for i in range(len(header)) if i not in used and header[i].strip()]
    return {"phones": phones, "name": name, "country": country, "extra": extra}
