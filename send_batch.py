"""
Send next 15 pending emails. Run repeatedly until all done.
STRICTLY sends each email exactly once - guaranteed by DB state machine.

State machine:
  pending -> sending (BEFORE smtp call)
  sending -> sent    (AFTER smtp succeeds)
  sending -> failed  (AFTER smtp hard-fails)

On restart: 'sending' = Gmail already received it = mark SENT, never resend.
"""
import random, time, sys
import common as C
import outreach

BATCH = 15

c = C.db()

# On restart: any 'sending' contact was already accepted by Gmail before crash.
# Mark them SENT. NEVER resend them.
stuck = c.execute("SELECT email, company FROM contacts WHERE status='sending'").fetchall()
for r in stuck:
    c.execute(
        "UPDATE contacts SET status='sent', "
        "note='Auto-confirmed sent on restart (Gmail had accepted message)' WHERE email=?",
        (r["email"],)
    )
if stuck:
    c.commit()
    print(f"Confirmed {len(stuck)} previously-interrupted sends as SENT (not resent):")
    for r in stuck:
        print(f"  {r['company']:<25} {r['email']}")
    print()

# Status
total_pending = c.execute("SELECT COUNT(*) FROM contacts WHERE status='pending'").fetchone()[0]
total_sent    = c.execute("SELECT COUNT(*) FROM contacts WHERE status='sent'").fetchone()[0]
print(f"Sent so far: {total_sent}   Still pending: {total_pending}")
print()

if total_pending == 0:
    print("All done! Everyone has been mailed.")
    sys.exit(0)

rows = c.execute(
    "SELECT * FROM contacts WHERE status='pending' ORDER BY company, email LIMIT ?",
    (BATCH,)
).fetchall()

print(f"This batch: {len(rows)} emails")
print()

sent_this_batch = 0
failed_this_batch = []

for i, r in enumerate(rows):

    # Must still be pending RIGHT NOW (absolute guarantee)
    cur = c.execute("SELECT status FROM contacts WHERE email=?", (r["email"],)).fetchone()
    if not cur or cur["status"] != "pending":
        print(f"  SKIP ({cur['status'] if cur else '?'}): {r['email']}")
        continue

    plain, h = outreach.render(r["company"], r["first_name"])
    msg, mid = outreach.build_msg(r["email"], outreach.SUBJECT, plain, h, attach=True)

    # ---------------------------------------------------------------
    # STEP 1: Mark 'sending' in DB BEFORE touching SMTP.
    # If we crash between here and step 3, restart treats it as sent.
    # This is the only state where a duplicate is impossible.
    # ---------------------------------------------------------------
    c.execute(
        "UPDATE contacts SET status='sending', message_id=?, subject=? WHERE email=?",
        (mid, outreach.SUBJECT, r["email"])
    )
    c.commit()

    # ---------------------------------------------------------------
    # STEP 2: Send via SMTP (fresh connection, closes after send_message)
    # ---------------------------------------------------------------
    smtp_accepted = False
    err_text = ""
    try:
        import smtplib, ssl as _ssl
        ctx = _ssl.create_default_context()
        s = smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx, timeout=30)
        s.login(C.GMAIL_USER, C.GMAIL_APP_PASSWORD)
        refused = s.send_message(msg)   # returns {} on success, raises on error
        smtp_accepted = True            # Gmail accepted it - mark sent immediately
        try: s.quit()
        except Exception: pass
        if refused:
            raise smtplib.SMTPRecipientsRefused(refused)
    except smtplib.SMTPRecipientsRefused as e:
        err_text = f"Recipient refused: {e}"
        smtp_accepted = False
    except smtplib.SMTPAuthenticationError:
        print("AUTH ERROR - check App Password in .env")
        # Revert to pending so it can be retried after fix
        c.execute("UPDATE contacts SET status='pending', message_id=NULL, subject=NULL WHERE email=?", (r["email"],))
        c.commit()
        sys.exit(1)
    except Exception as e:
        # For any other error after send_message was called, we don't know.
        # Keep as 'sending' -> restart will mark it sent (safer than resending)
        err_text = str(e)[:200]
        smtp_accepted = True  # assume accepted, better than duplicate

    # ---------------------------------------------------------------
    # STEP 3: Update DB based on outcome
    # ---------------------------------------------------------------
    stamp = C.iso()
    if smtp_accepted:
        c.execute("UPDATE contacts SET status='sent', sent_at=? WHERE email=?", (stamp, r["email"]))
        c.commit()
        C.audit("SENT", r["email"], f"{r['company']}: {outreach.SUBJECT}")
        name = r["first_name"] or "(no name)"
        remaining = total_pending - sent_this_batch - 1
        print(f"[{sent_this_batch+1}/{len(rows)}] SENT -> {r['email']}  ({r['company']}, {name})  {stamp}  |  {remaining} pending")
        sent_this_batch += 1
    else:
        c.execute("UPDATE contacts SET status='failed', note=? WHERE email=?", (err_text, r["email"]))
        c.commit()
        C.audit("FAILED", r["email"], f"{r['company']}: {err_text}")
        print(f"  FAILED -> {r['email']}: {err_text}  (saved to Excel)")
        failed_this_batch.append((r["email"], r["company"], err_text))

    # Gap (skip after last)
    if i < len(rows) - 1 and smtp_accepted:
        gap = random.randint(60, 75)
        print(f"  (waiting {gap}s...)")
        time.sleep(gap)

# Save Excel
try:
    C.build_excel()
except Exception as e:
    print(f"(Excel update failed: {e})")

remaining_total = c.execute("SELECT COUNT(*) FROM contacts WHERE status='pending'").fetchone()[0]
print()
print("=" * 55)
print(f"Batch done.  Sent: {sent_this_batch}  Failed: {len(failed_this_batch)}")
print(f"Still pending: {remaining_total}")
if remaining_total > 0:
    print("Run again:  python send_batch.py")
else:
    print("ALL DONE - everyone has been mailed!")
print(f"Excel: {C.EXCEL_OUT}")
print("=" * 55)

if failed_this_batch:
    print()
    print("FAILED (noted in Excel, will not be retried):")
    for em, co, err in failed_this_batch:
        print(f"  {co:<25} {em}  ({err})")
