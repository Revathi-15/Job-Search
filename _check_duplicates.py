import common as C

c = C.db()

# Check audit log for anyone who was sent to more than once
print("=== CONTACTS SENT MORE THAN ONCE (audit log) ===")
rows = c.execute("""
    SELECT target, COUNT(*) n, GROUP_CONCAT(ts, ' | ') as times
    FROM audit
    WHERE action='SENT'
    GROUP BY target
    HAVING COUNT(*) > 1
    ORDER BY n DESC
""").fetchall()

if rows:
    for r in rows:
        print(f"  DUPLICATE: {r['target']}  sent {r['n']} times")
        print(f"    at: {r['times']}")
else:
    print("  None found - no duplicates in audit log")

print()
print("=== TOTAL SENT (audit log) ===")
total = c.execute("SELECT COUNT(*) FROM audit WHERE action='SENT'").fetchone()[0]
print(f"  {total} SENT entries in audit log")

print()
print("=== CONTACTS TABLE sent status ===")
sent = c.execute("SELECT COUNT(*) FROM contacts WHERE status='sent'").fetchone()[0]
print(f"  {sent} contacts marked sent in DB")
