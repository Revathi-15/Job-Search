import common as C
from collections import Counter

c = C.db()
rows = c.execute(
    "SELECT email, status, sent_at, note FROM contacts WHERE lower(company) LIKE '%airbase%' ORDER BY status"
).fetchall()

print(f"Total Airbase contacts: {len(rows)}")
print()
counts = Counter(r[1] for r in rows)
for status, n in counts.items():
    print(f"  {status}: {n}")
print()
for r in rows:
    print(f"  [{r[1]}] {r[0]}  sent={r[2]}  note={r[3]}")
