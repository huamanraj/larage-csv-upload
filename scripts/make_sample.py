"""Generate a contacts CSV in the import schema: phone, name, country (+ extra columns kept as vars).

email and birthday are checked fields: ~5% of e-mails are malformed and ~5% of birthdays are placeholders or
impossible dates, so those values are blanked (the row is still imported).

python scripts/make_sample.py 100000 sample.csv [seed]   (a different seed makes a different file)
"""
import csv
import random
import sys

NAMES = ["Aarav Sharma", "Diya Iyer", "Rohan Reddy", "Meera Nair", "Kabir Patel", "Priya Gupta", "Arjun Mehta",
         "Sneha Rao", "Vikram Singh", "Pooja Das", "Karan Kulkarni", "Riya Verma"]
CITIES = ["Mumbai", "Delhi", "Bengaluru", "Chennai", "Pune", "Hyderabad", "Kolkata", "Jaipur"]
COMPANIES = ["Acme Retail", "Nimbus Labs", "Sagar Foods", "Orbit Logistics", "Kite Finance", "Lotus Health"]

FOREIGN = [  # (country cell, local-format number)
    ("United States", lambda r: f"({r.randrange(201, 989)}) {r.randrange(200, 999)}-{r.randrange(1000, 9999)}"),
    ("UK", lambda r: f"07{r.randrange(100, 999)} {r.randrange(100000, 999999)}"),
    ("UAE", lambda r: f"05{r.choice('024568')} {r.randrange(100, 999)} {r.randrange(1000, 9999)}"),
    ("Singapore", lambda r: f"{r.choice('89')}{r.randrange(100, 999)} {r.randrange(1000, 9999)}"),
]


def indian(r):
    n = f"{r.choice('6789')}{r.randrange(10**8, 10**9)}"
    return r.choice([n, f"+91 {n[:5]} {n[5:]}", f"0{n}", f"91-{n}"])


def messy(r):
    return r.choice(["", "", f"9.19{r.randrange(100, 999)}E+11", str(r.randrange(1000, 99999)), "n/a"])


def email(r, name):
    if r.random() < 0.05:
        return r.choice(["n/a", "no-email", f"{name.split()[0].lower()}@", "abc@@x.com"])
    return f"{name.split()[0].lower()}.{r.randrange(1, 9999)}@{r.choice(['gmail.com', 'yahoo.co.in', 'outlook.com'])}"


def birthday(r):
    if r.random() < 0.05:
        return r.choice(["0/0/00", "31/02/1990", "unknown", ""])
    return f"{r.randrange(1950, 2005)}-{r.randrange(1, 13):02d}-{r.randrange(1, 29):02d}"


def main(n, out, seed=7):
    r = random.Random(seed)
    pool = [indian(r) for _ in range(max(1, int(n * 0.9)))]  # ~10% repeats -> duplicates
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["phone", "name", "country", "city", "company", "email", "birthday"])
        for _ in range(n):
            x = r.random()
            if x < 0.08:
                country, make = r.choice(FOREIGN)
                phone = make(r)
            else:
                country, phone = "India", (r.choice(pool) if x < 0.92 else messy(r))
            name = r.choice(NAMES)
            w.writerow([phone, name, country, r.choice(CITIES), r.choice(COMPANIES), email(r, name), birthday(r)])


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 100000, sys.argv[2] if len(sys.argv) > 2 else "sample.csv",
         int(sys.argv[3]) if len(sys.argv) > 3 else 7)
