"""Read the Excel sheets raw and show every Airbnb-related row, then compare with DB."""
from openpyxl import load_workbook
import re
import common as C

EMAIL_RX = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")

files = [
    "Companies_and_Employees_Emails.xlsx",
    "Employee_Emails_List_MNC_s.xlsx",
]

print("=" * 60)
print("RAW AIRBNB EMAILS FOUND IN EXCEL FILES")
print("=" * 60)

all_airbnb_emails = []

for fname in files:
    path = C.BASE / fname
    if not path.exists():
        print(f"  NOT FOUND: {fname}")
        continue
    wb = load_workbook(path, read_only=True, data_only=True)
    for ws in wb.worksheets:
        rows = list(ws.iter_rows(values_only=True))
        for i, row in enumerate(rows):
            row_text = " ".join(str(c) for c in row if c is not None)
            if "airbnb" in row_text.lower():
                emails = EMAIL_RX.findall(row_text)
                for em in emails:
                    em = em.lower().strip(".")
                    all_airbnb_emails.append(em)
                    print(f"  [{fname} | {ws.title} | row {i+1}] {em}")
                    print(f"    full row: {[c for c in row if c is not None]}")

print()
print(f"Total Airbnb emails in Excel: {len(all_airbnb_emails)}")
print(f"Unique: {len(set(all_airbnb_emails))}")

# Compare with DB
print()
print("=" * 60)
print("AIRBNB EMAILS IN DB vs EXCEL")
print("=" * 60)
c = C.db()
for em in sorted(set(all_airbnb_emails)):
    r = c.execute("SELECT email, company, status, note FROM contacts WHERE email=?", (em,)).fetchone()
    if r:
        print(f"  IN DB   [{r['status']}] {em}  company={r['company']}  note={r['note']}")
    else:
        print(f"  MISSING {em}  <-- NOT IN DB")
