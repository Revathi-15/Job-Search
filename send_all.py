"""
Send outreach emails to ALL pending contacts.
- Never sends twice - checks DB status right before every send
- Fresh SMTP connection per email - no timeout duplicates
- On any interruption: marks uncertain, collects all, shows at end
- Run: python send_all.py
"""
import random
import time
import sys
import os
import common as C
import outreach

# ------------------------------------------------------------------ lock
LOCK_FILE = C.BASE / "send_all.lock"
if LOCK_FILE.exists():
    age = time.time() - LOCK_FILE.stat().st_mtime
    if age < 6 * 3600:
        print(f"ERROR: Another send_all.py is already running.")
        print(f"If you are sure no other run is active, delete: {LOCK_FILE}")
        sys.exit(1)
    LOCK_FILE.unlink()
LOCK_FILE.write_text(str(os.getpid()))

uncertain_cases = []   # collect all uncertain - shown at end, never mid-run

try:
    # ---- reset any leftover 'sending' from previous interrupted run ----
    c = C.db()
    stuck = c.execute("SELECT email, company FROM contacts WHERE status='sending'").fetchall()
    for r in stuck:
        c.execute(
            "UPDATE contacts SET status='uncertain', "
            "note='Previous run interrupted mid-send - check Gmail Sent before retrying' "
            "WHERE email=?", (r["email"],)
        )
        uncertain_cases.append((r["email"], r["company"], "leftover from previous interrupted run"))
    if stuck:
        c.commit()
        print(f"NOTE: {len(stuck)} contacts from a previous interrupted run moved to uncertain - will show at end")
    print()

    # ---- sync ----
    total, new, _ = C.sync_contacts()
    print(f"Contacts in DB: {total}  ({new} new this run)")
    print()

    for r in c.execute("SELECT status, COUNT(*) n FROM contacts GROUP BY status ORDER BY n DESC"):
        print(f"  {r[0]:<12} {r[1]}")
    print()

    # ---- queue: only 'pending' ----
    rows = c.execute(
        "SELECT * FROM contacts WHERE status='pending' ORDER BY company, email"
    ).fetchall()

    print(f"Sending to: {len(rows)} contacts across {len(set(r['company'] for r in rows))} companies")
    print()

    if not rows:
        print("Nothing to send - all contacts already mailed or skipped.")
        sys.exit(0)

    sent = 0
    failed = 0

    for i, r in enumerate(rows):

        # re-check status right now - absolute guarantee no duplicate
        current = c.execute(
            "SELECT status FROM contacts WHERE email=?", (r["email"],)
        ).fetchone()
        if not current or current["status"] != "pending":
            print(f"  SKIP (already {current['status'] if current else 'missing'}): {r['email']}")
            continue

        # own addresses - never send
        if r["email"].lower() in ("sangamrevathi05@gmail.com", "revathisangam2005@gmail.com"):
            c.execute("UPDATE contacts SET status='skipped', note='Own email' WHERE email=?", (r["email"],))
            c.commit()
            continue

        plain, h = outreach.render(r["company"], r["first_name"])
        msg, mid = outreach.build_msg(r["email"], outreach.SUBJECT, plain, h, attach=True)

        # mark 'sending' before SMTP call - if we crash here it becomes uncertain, never pending
        c.execute(
            "UPDATE contacts SET status='sending', message_id=?, subject=? WHERE email=?",
            (mid, outreach.SUBJECT, r["email"])
        )
        c.commit()

        try:
            # fresh connection every time - no reuse, no timeout duplicate
            outreach.Mailer().send(msg)
            stamp = C.iso()
            c.execute("UPDATE contacts SET status='sent', sent_at=? WHERE email=?", (stamp, r["email"]))
            c.commit()
            C.audit("SENT", r["email"], f"{r['company']}: {outreach.SUBJECT}")
            name_label = r["first_name"] if r["first_name"] else "(no name)"
            print(f"[{sent+1}/{len(rows)}] SENT -> {r['email']}  ({r['company']}, {name_label})  {stamp}  |  {len(rows)-sent-1} left")
            sent += 1

            if sent % 10 == 0:
                try:
                    C.build_excel()
                    print(f"  [Excel saved - {sent} sent]")
                except Exception:
                    pass

            if i < len(rows) - 1:
                gap = random.randint(60, 90)
                print(f"  (waiting {gap}s...)")
                time.sleep(gap)

        except KeyboardInterrupt:
            # interrupted during send - don't know if it went - mark uncertain, continue
            c.execute(
                "UPDATE contacts SET status='uncertain', "
                "note='Interrupted during send - check Gmail Sent folder' WHERE email=?",
                (r["email"],)
            )
            c.commit()
            uncertain_cases.append((r["email"], r["company"], "interrupted during send"))
            print(f"  UNCERTAIN (interrupted): {r['email']} - will show at end, continuing...")
            # wait a moment then continue to next contact
            time.sleep(2)
            continue

        except Exception as e:
            outcome, stop, text = outreach.classify_error(e)
            if outcome == "failed":
                c.execute("UPDATE contacts SET status='failed', note=? WHERE email=?", (text, r["email"]))
                c.commit()
                C.audit("FAILED", r["email"], f"{r['company']}: {text}")
                print(f"  FAILED -> {r['email']}: {text}")
                failed += 1
            else:
                # uncertain - don't retry automatically
                c.execute(
                    "UPDATE contacts SET status='uncertain', note=? WHERE email=?",
                    ("Send uncertain: " + text, r["email"])
                )
                c.commit()
                uncertain_cases.append((r["email"], r["company"], text))
                print(f"  UNCERTAIN: {r['email']} ({text}) - will show at end")
                if stop:
                    print(f"  Critical error - stopping run. Run again to continue.")
                    break

    # ---- final Excel update ----
    try:
        C.build_excel()
    except Exception:
        pass

    # ---- summary ----
    print()
    print("=" * 60)
    print(f"DONE.  Sent: {sent}   Failed: {failed}   Uncertain: {len(uncertain_cases)}")
    print(f"Excel: {C.EXCEL_OUT}")
    print("=" * 60)

    if uncertain_cases:
        print()
        print("UNCERTAIN EMAILS - CHECK THESE MANUALLY IN GMAIL SENT FOLDER:")
        print("(If the email IS in Sent -> it went fine, tell me and I'll mark it sent)")
        print("(If NOT in Sent -> tell me and I'll resend that one)")
        print()
        for em, co, reason in uncertain_cases:
            print(f"  {co:<25} {em}")
            print(f"    reason: {reason}")

finally:
    try:
        LOCK_FILE.unlink()
    except Exception:
        pass
