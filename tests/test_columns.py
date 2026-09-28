from app.columns import resolve

OUTLOOK = ("Title,First Name,Middle Name,Last Name,Suffix,Company,Number,Business Country/Region,Home Country/Region,"
           "Business Fax,Business Phone,Home Phone,Mobile Phone,Pager,Primary Phone,Government ID Number,"
           "E-mail Address,E-mail Display Name").split(",")


def names(header, idx):
    return [header[i] for i in idx]


def test_simple_schema():
    c = resolve(["Phone", "Name", "Country", "City"])
    assert c == {"phones": [0], "name": [1], "country": [2], "extra": [("City", 3)]}


def test_outlook_export():
    c = resolve(OUTLOOK)
    assert names(OUTLOOK, c["phones"]) == ["Mobile Phone", "Number", "Primary Phone", "Business Phone", "Home Phone"]
    assert names(OUTLOOK, c["name"]) == ["First Name", "Middle Name", "Last Name"]
    assert names(OUTLOOK, c["country"]) == ["Home Country/Region", "Business Country/Region"]
    extra = [n for n, _ in c["extra"]]
    assert "Business Fax" in extra and "Pager" in extra and "Government ID Number" in extra  # never phones
    assert "Company" in extra and "E-mail Address" in extra


def test_no_phone_column():
    assert resolve(["name", "email", "fax"])["phones"] == []
