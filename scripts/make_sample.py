"""Generate an Outlook-style contacts CSV with messy phone data.

python scripts/make_sample.py 100000 sample.csv
"""
import csv
import random
import sys

FIRST = ["Aarav", "Vivaan", "Aditya", "Diya", "Ananya", "Ishaan", "Kavya", "Rohan", "Saanvi", "Arjun", "Meera", "Kabir",
         "Priya", "Neha", "Rahul", "Sneha", "Vikram", "Pooja", "Karan", "Riya"]
LAST = ["Sharma", "Verma", "Iyer", "Reddy", "Nair", "Patel", "Gupta", "Mehta", "Rao", "Singh", "Das", "Kulkarni"]
CITIES = ["Mumbai", "Delhi", "Bengaluru", "Chennai", "Pune", "Hyderabad", "Kolkata", "Jaipur", "Kochi", "Indore"]
COMPANIES = ["Acme Retail", "Nimbus Labs", "Sagar Foods", "Orbit Logistics", "Kite Finance", "Lotus Health"]
HEADERS = ["First Name", "Middle Name", "Last Name", "Title", "Suffix", "Nickname", "E-mail Address", "E-mail 2 Address",
           "Mobile Phone", "Primary Phone", "Home Phone", "Business Phone", "Business Fax", "Company", "Department",
           "Job Title", "Home Street", "Home City", "Home State", "Home Postal Code", "Home Country",
           "Business Street", "Business City", "Business State", "Birthday", "Notes", "Categories", "Web Page"]


def mobile(rng):
    n = f"{rng.choice('6789')}{rng.randrange(10**8, 10**9)}"
    style = rng.random()
    if style < 0.35:
        return n
    if style < 0.60:
        return f"+91 {n[:5]} {n[5:]}"
    if style < 0.75:
        return f"0{n}"
    if style < 0.85:
        return f"91-{n}"
    return f"({n[:3]}) {n[3:6]}-{n[6:]}"


def messy(rng):
    r = rng.random()
    if r < 0.04:
        return f"9.19{rng.randrange(100, 999)}E+11"  # Excel scientific notation
    if r < 0.07:
        return f"0{rng.choice(['80', '22', '11', '44'])} {rng.randrange(2000, 9999)} {rng.randrange(1000, 9999)}"
    if r < 0.10:
        return str(rng.randrange(1000, 999999))
    if r < 0.12:
        return f"+1 415 555 {rng.randrange(1000, 9999)}"
    return ""


def main(n, out):
    rng = random.Random(7)
    pool = [mobile(rng) for _ in range(max(1, int(n * 0.9)))]  # ~10% repeats -> duplicates
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(HEADERS)
        for i in range(n):
            fn, ln, city = rng.choice(FIRST), rng.choice(LAST), rng.choice(CITIES)
            r = rng.random()
            mob = rng.choice(pool) if r < 0.80 else ("" if r < 0.90 else messy(rng))
            prim = mobile(rng) if (not mob and rng.random() < 0.5) else ("" if rng.random() < 0.7 else messy(rng))
            w.writerow([fn, "", ln, "", "", "", f"{fn}.{ln}{i}@example.com".lower(), "", mob, prim, "",
                        messy(rng) if rng.random() < 0.3 else "", "", rng.choice(COMPANIES), "", "Manager",
                        f"{rng.randrange(1, 300)} MG Road", city, "", str(rng.randrange(400001, 700000)), "India",
                        "", city, "", "", "", "Leads", ""])


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 100000, sys.argv[2] if len(sys.argv) > 2 else "sample.csv")
