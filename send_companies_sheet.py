"""Send outreach emails to all pending contacts from Companies_and_Employees_Emails.xlsx"""
import random
import time
import common as C
import outreach

# Sync contacts from all xlsx files into DB first
total, new, skipped_load = C.sync_contacts()
print(f"Contacts synced: {total} total, {new} new, {skipped_load} skipped (personal/no-reply)")
print()

c = C.db()

# Only pending contacts sourced from Companies_and_Employees_Emails.xlsx
rows = c.execute(
    "SELECT * FROM contacts "
    "WHERE status='pending' "
    "AND source LIKE '%Companies_and_Employees_Emails%' "
    "ORDER BY company, email"
).fetchall()

from collections import Counter
all_statuses = c.execute(
    "SELECT status, COUNT(*) n FROM contacts "
    "WHERE source LIKE '%Companies_and_Employees_Emails%' "
    "GROUP BY status ORDER BY n DESC"
).fetchall()

print("Status breakdown for Companies_and_Employees_Emails.xlsx:")
for r in all_statuses:
    print(f"  {r[0]}: {r[1]}")
print()
print(f"Pending contacts to send: {len(rows)}")
print()

if not rows:
    print("Nothing to send - all contacts already mailed or skipped.")
    raise SystemExit(0)

mailer = outreach.Mailer()
sent = 0
failed = 0

try:
    for i, r in enumerate(rows):
        plain, h = outreach.render(r["company"], r["first_name"])
        msg, mid = outreach.build_msg(r["email"], outreach.SUBJECT, plain, h, attach=True)

        # record BEFORE sending - crash-safe, never sends twice
        c.execute(
            "UPDATE contacts SET status='sending', message_id=?, subject=? WHERE email=?",
            (mid, outreach.SUBJECT, r["email"])
        )
        c.commit()

        try:
            mailer.send(msg)
            stamp = C.iso()
            c.execute(
                "UPDATE contacts SET status='sent', sent_at=? WHERE email=?",
                (stamp, r["email"])
            )
            c.commit()
            C.audit("SENT", r["email"], f"{r['company']}: {outreach.SUBJECT}")
            name_label = r["first_name"] if r["first_name"] else "(no name)"
            print(f"[{sent+1}/{len(rows)}] SENT -> {r['email']}  ({r['company']}, {name_label})  at {stamp}")
            sent += 1

            if i < len(rows) - 1:
                gap = random.randint(60, 90)
                print(f"  waiting {gap}s...")
                time.sleep(gap)

        except Exception as e:
            outcome, stop, text = outreach.classify_error(e)
            c.execute(
                "UPDATE contacts SET status='failed', note=? WHERE email=?",
                (text, r["email"])
            )
            c.commit()
            C.audit("FAILED", r["email"], f"{r['company']}: {text}")
            print(f"  FAILED -> {r['email']}: {text}")
            failed += 1
            if stop:
                print(f"Stopping due to critical error: {text}")
                break

finally:
    mailer.close()

print(f"\n{'='*50}")
print(f"Done. Sent: {sent}  Failed: {failed}  Remaining: {len(rows)-sent-failed}")
C.build_excel()
print(f"Excel updated: {C.EXCEL_OUT}")
