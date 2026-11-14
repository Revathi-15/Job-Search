import common as C
from collections import Counter

C.sync_contacts()
c = C.db()

# Overall DB status
print("=" * 55)
print("OVERALL STATUS")
print("=" * 55)
all_rows = c.execute("SELECT status, COUNT(*) n FROM contacts GROUP BY status ORDER BY n DESC").fetchall()
for r in all_rows:
    print(f"  {r[0]:<12} {r[1]}")

# Companies_and_Employees_Emails.xlsx breakdown
print()
print("=" * 55)
print("Companies_and_Employees_Emails.xlsx")
print("=" * 55)
rows = c.execute(
    "SELECT status, COUNT(*) n FROM contacts "
    "WHERE source LIKE '%Companies_and_Employees_Emails%' "
    "GROUP BY status ORDER BY n DESC"
).fetchall()
for r in rows:
    print(f"  {r[0]:<12} {r[1]}")

# Recently sent
print()
print("=" * 55)
print("LAST 10 SENT")
print("=" * 55)
sent = c.execute(
    "SELECT email, company, sent_at FROM contacts "
    "WHERE status='sent' ORDER BY sent_at DESC LIMIT 10"
).fetchall()
for r in sent:
    print(f"  {r[2]}  {r[1]:<25} {r[0]}")

# Uncertain (interrupted)
uncertain = c.execute(
    "SELECT email, company, note FROM contacts WHERE status='uncertain'"
).fetchall()
if uncertain:
    print()
    print("=" * 55)
    print("UNCERTAIN (run interrupted mid-send - check Gmail Sent)")
    print("=" * 55)
    for r in uncertain:
        print(f"  {r[1]:<25} {r[0]}")
