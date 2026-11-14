"""Full audit: every email in both Excel files vs DB. Shows any missing."""
from openpyxl import load_workbook
import re, common as C
from collections import Counter

EMAIL_RX = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")

files = ["Companies_and_Employees_Emails.xlsx", "Employee_Emails_List_MNC_s.xlsx"]

excel_emails = set()
for fname in files:
    path = C.BASE / fname
    if not path.exists():
        continue
    wb = load_workbook(path, read_only=True, data_only=True)
    for ws in wb.worksheets:
        for row in ws.iter_rows(values_only=True):
            for cell in row:
                if isinstance(cell, str):
                    for em in EMAIL_RX.findall(cell):
                        excel_emails.add(em.lower().strip("."))

print(f"Total unique emails in Excel files: {len(excel_emails)}")

# Sync to DB
C.sync_contacts()
c = C.db()

missing = []
for em in sorted(excel_emails):
    r = c.execute("SELECT status FROM contacts WHERE email=?", (em,)).fetchone()
    if not r:
        missing.append(em)

print(f"Missing from DB: {len(missing)}")
for em in missing:
    print(f"  MISSING: {em}")

# Full status breakdown
print()
print("DB STATUS BREAKDOWN:")
for r in c.execute("SELECT status, COUNT(*) n FROM contacts GROUP BY status ORDER BY n DESC"):
    print(f"  {r[0]:<12} {r[1]}")

print()
print("PENDING BY COMPANY (top 20):")
for r in c.execute("SELECT company, COUNT(*) n FROM contacts WHERE status='pending' GROUP BY company ORDER BY n DESC LIMIT 20"):
    print(f"  {r['company']:<30} {r['n']}")
