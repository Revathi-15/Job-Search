"""Fix DB after duplicate sends - mark uncertain/sent correctly, reset stuck 'sending'."""
import common as C

c = C.db()

# 1. Reset any stuck 'sending' -> 'uncertain'
stuck = c.execute("SELECT email, company FROM contacts WHERE status='sending'").fetchall()
for r in stuck:
    c.execute(
        "UPDATE contacts SET status='uncertain', note='Interrupted mid-send - check Gmail Sent' WHERE email=?",
        (r["email"],)
    )
    print(f"  Reset stuck: {r['email']} ({r['company']})")
c.commit()

# 2. Find everyone who appears in audit log as SENT more than once
dupes = c.execute("""
    SELECT target, COUNT(*) n, MAX(ts) last_sent
    FROM audit WHERE action='SENT'
    GROUP BY target HAVING COUNT(*) > 1
""").fetchall()

print(f"\nContacts with duplicate sends: {len(dupes)}")
for r in dupes:
    print(f"  {r['target']} - sent {r['n']} times, last at {r['last_sent']}")
    # Make sure they're marked 'sent' in DB (not pending again)
    c.execute(
        "UPDATE contacts SET status='sent', note='WARNING: sent multiple times due to parallel run bug' WHERE email=? AND status!='sent'",
        (r["target"],)
    )
c.commit()

# 3. Final counts
print()
print("DB status after fix:")
for r in c.execute("SELECT status, COUNT(*) n FROM contacts GROUP BY status ORDER BY n DESC"):
    print(f"  {r[0]:<12} {r[1]}")

print()
print("Done. The lock in send_all.py now prevents this from ever happening again.")
