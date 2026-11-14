"""Send outreach emails to all pending Airbase contacts."""
import random
import time
import common as C
import outreach

c = C.db()
rows = c.execute(
    "SELECT * FROM contacts WHERE lower(company) LIKE '%airbase%' AND status='pending'"
).fetchall()

print(f"Sending to {len(rows)} pending Airbase contacts...\n")

mailer = outreach.Mailer()
sent = 0
failed = 0

try:
    for i, r in enumerate(rows):
        plain, h = outreach.render(r["company"], r["first_name"])
        msg, mid = outreach.build_msg(r["email"], outreach.SUBJECT, plain, h, attach=True)

        # record BEFORE sending so a crash never causes a duplicate
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
            C.audit("SENT", r["email"], f"Airbase: {outreach.SUBJECT}")
            name_label = r["first_name"] if r["first_name"] else "(no name)"
            print(f"  SENT -> {r['email']}  name={name_label}  at {stamp}")
            sent += 1
            if i < len(rows) - 1:
                gap = random.randint(60, 90)
                print(f"    (waiting {gap}s before next...)")
                time.sleep(gap)
        except Exception as e:
            outcome, stop, text = outreach.classify_error(e)
            c.execute(
                "UPDATE contacts SET status='failed', note=? WHERE email=?",
                (text, r["email"])
            )
            c.commit()
            C.audit("FAILED", r["email"], f"Airbase: {text}")
            print(f"  FAILED -> {r['email']}: {text}")
            failed += 1
            if stop:
                print(f"Stopping due to critical error: {text}")
                break
finally:
    mailer.close()

print(f"\nDone. Sent: {sent}  Failed: {failed}")
C.build_excel()
print(f"Excel updated: {C.EXCEL_OUT}")
