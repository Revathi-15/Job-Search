import common as C
from collections import Counter

C.sync_contacts()
c = C.db()

# 1. Check if sangamrevathi05 is in DB
print("=== OWN EMAILS IN DB ===")
for em in ["sangamrevathi05@gmail.com", "revathisangam2005@gmail.com"]:
    r = c.execute("SELECT email, status, source FROM contacts WHERE email=?", (em,)).fetchone()
    if r:
        print(f"  FOUND: {r['email']}  status={r['status']}  source={r['source']}")
    else:
        print(f"  NOT IN DB: {em}")

# 2. Check Airbnb
print()
print("=== AIRBNB CONTACTS ===")
rows = c.execute(
    "SELECT email, status, source, note FROM contacts WHERE lower(company) LIKE '%airbnb%'"
).fetchall()
print(f"  Total: {len(rows)}")
for r in rows:
    print(f"  [{r['status']}] {r['email']}  source={r['source']}  note={r['note']}")

# 3. Full pending breakdown by company (Companies_and_Employees_Emails.xlsx)
print()
print("=== PENDING BY COMPANY (Companies_and_Employees_Emails.xlsx) ===")
rows = c.execute(
    "SELECT company, COUNT(*) n FROM contacts "
    "WHERE status='pending' AND source LIKE '%Companies_and_Employees_Emails%' "
    "GROUP BY company ORDER BY company"
).fetchall()
print(f"  Total pending: {sum(r[1] for r in rows)} across {len(rows)} companies")
for r in rows:
    print(f"  {r['company']:<35} {r['n']}")
