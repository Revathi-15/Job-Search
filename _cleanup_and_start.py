import common as C
import os

c = C.db()

# 1. Remove lock file if exists
lock = C.BASE / "send_all.lock"
if lock.exists():
    lock.unlink()
    print("Removed lock file")

# 2. Reset any stuck 'sending' back to pending
stuck = c.execute("SELECT email FROM contacts WHERE status='sending'").fetchall()
for r in stuck:
    c.execute("UPDATE contacts SET status='pending', message_id=NULL, subject=NULL WHERE email=?", (r["email"],))
c.commit()
print(f"Reset {len(stuck)} stuck 'sending' contacts back to pending")

# 3. Final status
print()
print("Current DB status:")
for r in c.execute("SELECT status, COUNT(*) n FROM contacts GROUP BY status ORDER BY n DESC"):
    print(f"  {r[0]}: {r[1]}")
