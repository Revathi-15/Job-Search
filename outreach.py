#!/usr/bin/env python3
"""Revathi's job-outreach automation.

  python outreach.py run          # check replies + send pending mails (inside Mon-Fri windows)
  python outreach.py send --dry-run
  python outreach.py followup --min-days 5
  python outreach.py replies | report | status | jobs

See README.md for setup.
"""
import argparse
import datetime as dt
import email
import email.policy
import html
import imaplib
import math
import random
import re
import smtplib
import ssl
import sys
import time
from collections import Counter
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr, parsedate_to_datetime

import os
import traceback

# pythonw (scheduler, no console) has no stdout/stderr: give it a harmless sink
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

import common as C
from common import audit, audit_once_today

# ------------------------------------------------------------------ mail content (approved by Revathi)
SUBJECT = ("Application for Software Development Engineer - Revathi Sangam | "
           "Amazon & Publicis Sapient Intern")

P_INTRO = ("I hope this email finds you well.")
P_EXP = ("I'm Revathi Sangam, a recent B.Tech graduate in Computer Science from NIT Durgapur (CGPA: 8.91). "
         "I have internship experience from Amazon and Publicis Sapient, where I worked across the SDLC, "
         "collaborated with cross-functional teams and stakeholders, prepared design documents, and delivered "
         "tasks end to end. I also gained hands-on experience in backend development, API design, debugging, "
         "testing, resolving tickets, and handling critical production issues.")
P_CTA = ("I'm reaching out to explore SDE opportunities at {company}. I've attached my resume and would "
         "appreciate your consideration for any suitable openings or a referral to the relevant hiring manager.")
P_ATTACH = "You can also find my work here: [[LI]] | [[GH]]."
P_THANKS = "Thank you for your time and consideration!"
SIGN = f"Best regards,\n{C.SENDER_NAME}\n{C.PHONE} | {C.GMAIL_USER}"

F_1 = ("I hope you're having a productive week! I'm following up on my earlier note regarding Software "
       "Development Engineer opportunities at {company}.")
F_2 = ("I remain very enthusiastic about the chance to contribute to your team. If you have a few minutes for a "
       "quick chat, I would welcome the opportunity.")
F_3 = "Thanks again for your time!"
SIGN_F = f"{C.SENDER_NAME}\n{C.PHONE} | {C.GMAIL_USER}"


def render(company, name, followup=False):
    greet = f"Hi {name}," if name else "Hi there,"
    if followup:
        paras = [greet, F_1.format(company=company), F_2, F_3, SIGN_F]
    else:
        paras = [greet, P_INTRO, P_EXP, P_CTA.format(company=company), P_ATTACH, P_THANKS, SIGN]
    plain = "\n\n".join(paras)
    plain = plain.replace("[[LI]]", f"LinkedIn ({C.LINKEDIN})").replace("[[GH]]", f"GitHub ({C.GITHUB})")
    h = "".join("<p>" + html.escape(p).replace("\n", "<br>") + "</p>" for p in paras)
    h = h.replace("[[LI]]", f'<a href="{C.LINKEDIN}">LinkedIn</a>').replace("[[GH]]", f'<a href="{C.GITHUB}">GitHub</a>')
    return plain, h
    return plain, h


def build_msg(to, subject, plain, html_body, reply_to=None, attach=True):
    m = EmailMessage()
    m["From"] = f"{C.SENDER_NAME} <{C.GMAIL_USER}>"
    m["To"] = to
    m["Subject"] = subject
    m["Date"] = formatdate(localtime=True)
    mid = make_msgid(domain="gmail.com")
    m["Message-ID"] = mid
    if reply_to:
        m["In-Reply-To"] = reply_to
        m["References"] = reply_to
    m.set_content(plain)
    m.add_alternative(html_body, subtype="html")
    if attach:
        m.add_attachment(C.RESUME.read_bytes(), maintype="application", subtype="pdf", filename=C.RESUME.name)
    return m, mid


class Mailer:
    def __init__(self):
        self.s = None

    def connect(self):
        self.s = smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=60)
        self.s.login(C.GMAIL_USER, C.GMAIL_APP_PASSWORD)

    def send(self, msg):
        """Fresh connection per message. Once Gmail accepts the message (sendmail returns),
        it is delivered — we mark it sent immediately before attempting to close."""
        ctx = ssl.create_default_context()
        s = smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx, timeout=30)
        s.login(C.GMAIL_USER, C.GMAIL_APP_PASSWORD)
        # send_message returns {} on success, raises on failure
        refused = s.send_message(msg)
        if refused:
            raise smtplib.SMTPRecipientsRefused(refused)
        # close quietly — delivery already confirmed above
        try:
            s.quit()
        except Exception:
            pass

    def close(self):
        try:
            if self.s:
                self.s.quit()
        except Exception:
            pass
        self.s = None


def classify_error(e):
    """-> (outcome 'failed'|'retry', stop_run, message)"""
    if isinstance(e, smtplib.SMTPAuthenticationError):
        return "retry", True, "Gmail login failed - check GMAIL_USER / GMAIL_APP_PASSWORD in .env"
    if isinstance(e, smtplib.SMTPRecipientsRefused):
        return "failed", False, str(e)[:200]
    if isinstance(e, smtplib.SMTPDataError):
        t = str(e).lower()
        if "quota" in t or "limit" in t or "5.4.5" in t:
            return "retry", True, "Gmail sending limit reached"
        return "failed", False, str(e)[:200]
    return "retry", True, f"Network/SMTP problem: {e}"[:200]


# ------------------------------------------------------------------ helpers
def sent_today(c):
    d = C.now().strftime("%Y-%m-%d")
    rows = c.execute("SELECT company FROM contacts WHERE substr(sent_at,1,10)=? UNION ALL "
                     "SELECT company FROM contacts WHERE substr(followup_at,1,10)=?", (d, d)).fetchall()
    by_company = Counter(r[0] for r in rows)
    return sum(by_company.values()), by_company


def build_queue(c, by_company):
    """Pending contacts, interleaved across companies, respecting the per-company daily cap."""
    groups = {}
    for r in c.execute("SELECT * FROM contacts WHERE status='pending' ORDER BY company, email"):
        groups.setdefault(r["company"], []).append(r)
    cap = {co: max(0, C.PER_COMPANY_DAILY - by_company.get(co, 0)) for co in groups}
    queue = []
    while True:
        added = False
        for co, lst in groups.items():
            if lst and cap[co] > 0:
                queue.append(lst.pop(0))
                cap[co] -= 1
                added = True
        if not added:
            return queue


def fix_stuck(c):
    """A mail marked 'sending' that never got confirmed (crash / Ctrl+C). Never resend automatically."""
    for r in c.execute("SELECT email, company FROM contacts WHERE status='sending'").fetchall():
        c.execute("UPDATE contacts SET status='uncertain', note='Run was interrupted while sending - "
                  "check Gmail Sent folder' WHERE email=?", (r["email"],))
        c.commit()
        audit("UNCERTAIN", r["email"], f"{r['company']}: run stopped mid-send; NOT retried to avoid a duplicate",
              f"Search Gmail 'Sent' for {r['email']}. If it is there, mark it OK in the Excel; if not, tell me to retry.")
    for r in c.execute("SELECT email, company FROM contacts WHERE followup_message_id IS NOT NULL "
                       "AND followup_at IS NULL AND (note IS NULL OR note NOT LIKE 'Follow-up%')").fetchall():
        c.execute("UPDATE contacts SET note='Follow-up status uncertain - check Gmail Sent folder' WHERE email=?",
                  (r["email"],))
        c.commit()
        audit("UNCERTAIN", r["email"], f"{r['company']}: follow-up was interrupted; NOT retried",
              f"Search Gmail 'Sent' for {r['email']} to see whether the follow-up went.")


def revert(c, kind, email_addr):
    if kind == "followup":
        c.execute("UPDATE contacts SET followup_message_id=NULL WHERE email=?", (email_addr,))
    else:
        c.execute("UPDATE contacts SET status='pending', message_id=NULL, subject=NULL WHERE email=?", (email_addr,))
    c.commit()


def deliver(c, items, kind, mailer, a):
    """Send to items one by one; obeys windows, daily limit, gaps. Returns number sent."""
    followup = kind == "followup"
    n = 0
    for k, r in enumerate(items):
        if not C.in_window():
            audit("WINDOW_CLOSED", "", f"Sending window ended after {n} mails this run",
                  "None - next run continues automatically" if getattr(a, "auto", False) else "")
            break
        total, _ = sent_today(c)
        if total >= C.DAILY_LIMIT:
            audit_once_today("DAILY_LIMIT_REACHED", "", f"Reached today's limit of {C.DAILY_LIMIT} mails",
                             "None - sending resumes next working day")
            break
        if getattr(a, "limit", None) and n >= a.limit:
            break
        plain, h = render(r["company"], r["first_name"], followup)
        subject = ("Re: " + r["subject"]) if followup else SUBJECT
        msg, mid = build_msg(r["email"], subject, plain, h,
                             reply_to=r["message_id"] if followup else None, attach=not followup)
        if followup:
            c.execute("UPDATE contacts SET followup_message_id=? WHERE email=?", (mid, r["email"]))
        else:
            c.execute("UPDATE contacts SET status='sending', message_id=?, subject=? WHERE email=?",
                      (mid, subject, r["email"]))
        c.commit()  # recorded BEFORE sending so a crash can never lead to a duplicate
        try:
            mailer.send(msg)
        except Exception as e:  # noqa: BLE001
            outcome, stop, text = classify_error(e)
            if outcome == "failed":
                if followup:
                    c.execute("UPDATE contacts SET note=? WHERE email=?", ("Follow-up failed: " + text, r["email"]))
                else:
                    c.execute("UPDATE contacts SET status='failed', note=? WHERE email=?", (text, r["email"]))
                c.commit()
                audit("FAILED", r["email"], f"{r['company']}: {text}", "None (address rejected) - will not retry")
            else:
                revert(c, kind, r["email"])
                audit("RUN_STOPPED", r["email"], text, "Fix the problem above, then run again")
            if stop:
                break
            continue
        stamp = C.iso()
        if followup:
            c.execute("UPDATE contacts SET followup_at=? WHERE email=?", (stamp, r["email"]))
            audit("FOLLOWUP_SENT", r["email"], f"{r['company']}: {subject}")
        else:
            c.execute("UPDATE contacts SET status='sent', sent_at=? WHERE email=?", (stamp, r["email"]))
            audit("SENT", r["email"], f"{r['company']}: {subject}")
        c.commit()
        n += 1
        if k < len(items) - 1:
            time.sleep(random.uniform(C.GAP_MIN, C.GAP_MAX))
    return n


def wait_for_window(a):
    """Returns True if sending may start now."""
    if C.in_window():
        return True
    nxt = C.next_window_start()
    nxt_s = nxt.strftime("%a %d %b, %I:%M %p IST") if nxt else "?"
    if getattr(a, "wait", False) and nxt:
        print(f"Outside sending window. Waiting until {nxt_s} (Ctrl+C to cancel) ...")
        while not C.in_window():
            time.sleep(30)
        return True
    if not getattr(a, "auto", False):
        audit("SKIPPED_OUTSIDE_WINDOW", "", f"Not a sending time. Next window: {nxt_s}",
              "Run again then, or use --wait to let it wait")
    else:
        print(f"[{C.now():%H:%M}] outside window, next: {nxt_s}")
    return False


# ------------------------------------------------------------------ commands
def cmd_send(a):
    c = C.db()
    total, new, skipped = C.sync_contacts()
    if new:
        audit("CONTACTS_LOADED", "", f"{total} unique emails in sheets, {new} new ({skipped} skipped as personal/no-reply)")
    fix_stuck(c)

    if a.test:
        plain, h = render("TestCompany", "Revathi")
        msg, _ = build_msg(a.test_email if a.test_email else C.GMAIL_USER, SUBJECT, plain, h)
        m = Mailer()
        try:
            m.send(msg)
        finally:
            m.close()
        audit("TEST_SENT", a.test_email if a.test_email else C.GMAIL_USER, "Test mail sent", "Open Gmail inbox and check how it looks")
        return 0

    today_n, by_company = sent_today(c)
    queue = build_queue(c, by_company)
    pending = c.execute("SELECT COUNT(*) FROM contacts WHERE status='pending'").fetchone()[0]

    if a.dry_run:
        skipped_n = c.execute("SELECT COUNT(*) FROM contacts WHERE status='skipped'").fetchone()[0]
        print(f"\nDRY RUN - nothing will be sent.\n  Unique contacts: {total}\n  Pending: {pending}"
              f"\n  Skipped (personal/no-reply): {skipped_n}"
              f"\n  Companies in today's queue: {len({r['company'] for r in queue})}"
              f"\n  Sent today so far: {today_n} (limit {C.DAILY_LIMIT}/day, {C.PER_COMPANY_DAILY}/company/day)"
              f"\n  Estimated working days to finish: {math.ceil(pending / max(1, C.DAILY_LIMIT))}"
              f"\n  Sending window open now: {C.in_window()}")
        for r in queue[:3]:
            plain, _ = render(r["company"], r["first_name"])
            print(f"\n{'=' * 70}\nTO: {r['email']}  (company: {r['company']})\nSUBJECT: {SUBJECT}\nATTACHMENT: {C.RESUME.name}\n\n{plain}")
        print(f"\nFirst 15 in queue: " + ", ".join(f"{r['email']} [{r['company']}]" for r in queue[:15]))
        return 0

    if not wait_for_window(a):
        return 0
    if not queue:
        audit("NOTHING_TO_SEND", "", "No pending contacts for today (all done, or per-company cap reached)")
        return 0
    if not C.GMAIL_APP_PASSWORD:
        print("GMAIL_APP_PASSWORD missing in .env - see README step 3.")
        return 1
    m = Mailer()
    try:
        sent = deliver(c, queue, "initial", m, a)
    finally:
        m.close()
    left = c.execute("SELECT COUNT(*) FROM contacts WHERE status='pending'").fetchone()[0]
    audit("SEND_SUMMARY", "", f"Sent {sent} this run, {sent_today(c)[0]} today, {left} still pending",
          "None - next run continues" if left else "All contacts mailed. Consider 'followup' after 5+ days")
    return 0


def cmd_followup(a):
    c = C.db()
    fix_stuck(c)
    if not a.dry_run:
        try:
            do_replies(c, force=True)  # never nag someone who already answered
        except Exception as e:  # noqa: BLE001
            audit("FOLLOWUP_ABORTED", "", f"Could not check replies first ({e})",
                  "Fix IMAP access (README) and retry - I won't follow up blindly")
            return 1
    cutoff = (C.now() - dt.timedelta(days=a.min_days)).isoformat(timespec="seconds")
    rows = c.execute("SELECT * FROM contacts WHERE status='sent' AND replied=0 AND followup_at IS NULL "
                     "AND followup_message_id IS NULL AND sent_at <= ? ORDER BY sent_at", (cutoff,)).fetchall()
    if a.dry_run:
        print(f"DRY RUN - {len(rows)} contacts eligible for a follow-up (mailed {a.min_days}+ days ago, no reply).")
        for r in rows[:3]:
            plain, _ = render(r["company"], r["first_name"], True)
            print(f"\n{'=' * 70}\nTO: {r['email']}\nSUBJECT: Re: {r['subject']}\n\n{plain}")
        return 0
    if not rows:
        audit("NOTHING_TO_SEND", "", f"No follow-ups due (need {a.min_days}+ days since first mail, no reply)")
        return 0
    if not wait_for_window(a):
        return 0
    audit("FOLLOWUP_START", "", f"{len(rows)} eligible contacts")
    m = Mailer()
    try:
        sent = deliver(c, rows, "followup", m, a)
    finally:
        m.close()
    audit("FOLLOWUP_SUMMARY", "", f"{sent} follow-ups sent, {len(rows) - sent} still eligible",
          "Run 'followup' again in the next window to continue" if len(rows) > sent else "")
    return 0


# ------------------------------------------------------------------ replies (IMAP)
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
AUTO_KEYS = ["out of office", "automatic reply", "auto-reply", "autoreply", "auto reply", "on leave",
             "away from", "vacation", "this is an automated", "do not reply to this"]
NEG_KEYS = ["not hiring", "no openings", "no open positions", "no current opening", "unfortunately", "regret",
            "not looking", "hiring freeze", "not currently hiring", "no vacancies"]
POS_KEYS = ["interested", "resume", "schedule", "call", "connect", "forward", "refer", "apply", "opening",
            "position", "happy to", "share", "next steps", "discuss"]


def body_text(msg):
    try:
        part = msg.get_body(preferencelist=("plain", "html"))
        if part is None:
            return ""
        txt = part.get_content()
        if part.get_content_type() == "text/html":
            txt = html.unescape(re.sub(r"<[^>]+>", " ", txt))
        return txt
    except Exception:  # noqa: BLE001
        return ""


def clean_snippet(txt):
    lines = txt.splitlines()
    keep = []
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s.startswith(">"):
            continue
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if (s.startswith("On ") and ("wrote:" in s or "wrote:" in nxt)) or \
                re.match(r"^(From:|-----Original Message|Sent from my)", s, re.I):
            break
        keep.append(s)
    return re.sub(r"\s+", " ", " ".join(keep)).strip()[:300]


def classify(msg, text):
    low = (text + " " + str(msg.get("Subject", ""))).lower()
    auto = (str(msg.get("Auto-Submitted", "no")).lower() != "no"
            or str(msg.get("Precedence", "")).lower() in ("auto_reply", "bulk", "junk")
            or any(k in low for k in AUTO_KEYS))
    if auto:
        return "Auto-reply (out of office)"
    if any(k in low for k in NEG_KEYS):
        return "Negative / no openings"
    if any(k in low for k in POS_KEYS):
        return "Positive - action needed"
    return "Reply - please read"


ACTIONS = {
    "Positive - action needed": "Reply to {e} soon - the response looks positive",
    "Reply - please read": "Read and reply to {e}",
    "Negative / no openings": "No action (they said no openings) - try other contacts at the company",
    "Auto-reply (out of office)": "No action - automatic reply; a human may still answer later",
}


def process_incoming(c, msg):
    """Match one inbox message to a contact. Returns 'reply', 'bounce' or None."""
    frm = parseaddr(str(msg.get("From", "")))[1].lower()
    if not frm or frm == C.GMAIL_USER.lower():
        return None

    if "mailer-daemon" in frm or "postmaster" in frm:
        blob = ""
        for part in msg.walk():
            if part.get_content_maintype() == "text":
                try:
                    blob += part.get_content()
                except Exception:  # noqa: BLE001
                    pass
            elif part.get_content_type() == "message/delivery-status":
                blob += str(part)
        hit = None
        for em in {e.lower().strip(".") for e in C.EMAIL_RX.findall(blob)}:
            r = c.execute("SELECT * FROM contacts WHERE email=? AND status='sent'", (em,)).fetchone()
            if r:
                c.execute("UPDATE contacts SET status='bounced', note='Bounced - address not deliverable' "
                          "WHERE email=?", (em,))
                c.commit()
                audit("BOUNCE_DETECTED", em, f"{r['company']}: email address rejected by their server",
                      "None - invalid address, it will not be retried")
                hit = "bounce"
        return hit

    refs = set((str(msg.get("In-Reply-To", "")) + " " + str(msg.get("References", ""))).split())
    row = None
    for ref in refs:
        row = c.execute("SELECT * FROM contacts WHERE message_id=? OR followup_message_id=?", (ref, ref)).fetchone()
        if row:
            break
    try:
        when = parsedate_to_datetime(str(msg.get("Date"))).astimezone(C.IST)
    except Exception:  # noqa: BLE001
        when = C.now()
    if not row:
        row = c.execute("SELECT * FROM contacts WHERE email=? AND sent_at IS NOT NULL", (frm,)).fetchone()
        if row and when < C.parse_iso(row["sent_at"]):
            row = None
    if not row:
        return None

    text = body_text(msg)
    rtype = classify(msg, text)
    if row["replied"] and not str(row["reply_type"]).startswith("Auto-reply"):
        return None  # first real reply already recorded
    if row["replied"] and rtype.startswith("Auto-reply"):
        return None
    snippet = clean_snippet(text)
    c.execute("UPDATE contacts SET replied=1, reply_at=?, reply_type=?, reply_snippet=? WHERE email=?",
              (C.iso(when), rtype, snippet, row["email"]))
    c.commit()
    audit("REPLY_DETECTED", row["email"], f"{row['company']}: {rtype} - {snippet[:140]}",
          ACTIONS[rtype].format(e=row["email"]))
    return "reply"


def do_replies(c, force=False):
    if not C.GMAIL_APP_PASSWORD:
        raise RuntimeError("GMAIL_APP_PASSWORD missing in .env")
    last = C.meta_get("last_reply_check")
    if not force and last and (C.now() - C.parse_iso(last)).total_seconds() < 30 * 60:
        return 0
    first = c.execute("SELECT MIN(sent_at) FROM contacts WHERE sent_at IS NOT NULL").fetchone()[0]
    if not first:
        C.meta_set("last_reply_check", C.iso())
        return 0
    since = C.parse_iso(first) - dt.timedelta(days=1)
    if last:
        since = max(since, C.parse_iso(last) - dt.timedelta(days=2))
    known = {r[0] for r in c.execute("SELECT email FROM contacts WHERE sent_at IS NOT NULL")}
    ours = set()
    for r in c.execute("SELECT message_id, followup_message_id FROM contacts"):
        ours.update(x for x in r if x)
    found = 0
    imap = imaplib.IMAP4_SSL("imap.gmail.com", 993)
    try:
        imap.login(C.GMAIL_USER, C.GMAIL_APP_PASSWORD)
        imap.select("INBOX", readonly=True)
        _, data = imap.uid("SEARCH", None, "SINCE", f"{since.day:02d}-{MONTHS[since.month - 1]}-{since.year}")
        uids = data[0].split()
        wanted = []
        for i in range(0, len(uids), 200):
            chunk = ",".join(u.decode() for u in uids[i:i + 200])
            _, resp = imap.uid("FETCH", chunk, "(UID BODY.PEEK[HEADER.FIELDS (FROM IN-REPLY-TO REFERENCES)])")
            pend = None
            for item in resp:
                if isinstance(item, tuple):
                    m = re.search(rb"UID (\d+)", item[0])
                    uid = m.group(1) if m else None
                    pend = None if uid else item
                    hdr = email.message_from_bytes(item[1])
                    frm = parseaddr(str(hdr.get("From", "")))[1].lower()
                    refs = set((str(hdr.get("In-Reply-To", "")) + " " + str(hdr.get("References", ""))).split())
                    if uid and (frm in known or "mailer-daemon" in frm or "postmaster" in frm or refs & ours):
                        wanted.append(uid)
        for uid in wanted:
            _, resp = imap.uid("FETCH", uid.decode(), "(BODY.PEEK[])")
            raw = next((x[1] for x in resp if isinstance(x, tuple)), None)
            if raw and process_incoming(c, email.message_from_bytes(raw, policy=email.policy.default)):
                found += 1
    finally:
        try:
            imap.logout()
        except Exception:  # noqa: BLE001
            pass
    C.meta_set("last_reply_check", C.iso())
    return found


def cmd_replies(a):
    c = C.db()
    try:
        n = do_replies(c, force=True)
    except Exception as e:  # noqa: BLE001
        audit("REPLY_CHECK_FAILED", "", str(e)[:200],
              "Check GMAIL_APP_PASSWORD and that IMAP is enabled in Gmail settings (README)")
        return 1
    audit("REPLY_CHECK", "", f"Inbox checked, {n} new replies/bounces recorded")
    return 0


def cmd_status(a):
    c = C.db()
    print("Contacts by status:")
    for r in c.execute("SELECT status, COUNT(*) n FROM contacts GROUP BY status ORDER BY n DESC"):
        print(f"  {r['status']:<10} {r['n']}")
    print(f"  replied    {c.execute('SELECT COUNT(*) FROM contacts WHERE replied=1').fetchone()[0]}")
    n, _ = sent_today(c)
    nxt = C.next_window_start()
    print(f"Sent today: {n}/{C.DAILY_LIMIT}   Window open now: {C.in_window()}   "
          f"Next window: {nxt:%a %d %b %I:%M %p} IST" if nxt else "")
    print(f"Jobs tracked: {c.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]}")
    return 0


def cmd_report(a):
    print(f"Excel updated: {C.build_excel()}")
    return 0


def cmd_jobs(a):
    import jobs
    return jobs.run_jobs(auto=getattr(a, "auto", False), notify=not a.no_notify)


def cmd_run(a):
    c = C.db()
    if not a.dry_run and not a.test and not C.GMAIL_APP_PASSWORD:
        audit_once_today("SETUP_NEEDED", "", "GMAIL_APP_PASSWORD is missing in .env, so nothing can be sent",
                         "Create a Gmail App Password and put it in the .env file (see README step 3)")
        return 0 if getattr(a, "auto", False) else 1
    if not a.dry_run and not a.test:
        try:
            do_replies(c)
        except Exception as e:  # noqa: BLE001
            audit("REPLY_CHECK_FAILED", "", str(e)[:200],
                  "Check GMAIL_APP_PASSWORD and that IMAP is enabled in Gmail settings (README)")
    return cmd_send(a)


# ------------------------------------------------------------------ main
def main(argv=None):
    p = argparse.ArgumentParser(description="Job outreach automation")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("run", "send"):
        s = sub.add_parser(name, help="send pending mails" + (" (after checking replies)" if name == "run" else ""))
        s.add_argument("--dry-run", action="store_true", help="show what would happen, send nothing")
        s.add_argument("--test", action="store_true", help="send ONE test mail")
        s.add_argument("--test-email", type=str, help="email address for test mail (default: your own)")
        s.add_argument("--limit", type=int, help="max mails this run")
        s.add_argument("--wait", action="store_true", help="if outside the window, wait for the next one")
        s.add_argument("--auto", action="store_true", help="used by the scheduler (quiet when idle)")
    f = sub.add_parser("followup", help="follow-up mail to people who got a mail 5+ days ago and did not reply")
    f.add_argument("--min-days", type=int, default=5)
    f.add_argument("--dry-run", action="store_true")
    f.add_argument("--limit", type=int)
    f.add_argument("--wait", action="store_true")
    f.add_argument("--auto", action="store_true")
    sub.add_parser("replies", help="check Gmail inbox for replies and bounces")
    sub.add_parser("status", help="quick counts")
    sub.add_parser("report", help="rebuild the Excel tracker")
    j = sub.add_parser("jobs", help="look for fresher openings and notify")
    j.add_argument("--auto", action="store_true")
    j.add_argument("--no-notify", action="store_true")
    a = p.parse_args(argv)

    quiet = getattr(a, "auto", False)
    lock = "jobs" if a.cmd == "jobs" else "outreach"
    try:
        with C.RunLock(lock):
            if not quiet and a.cmd in ("run", "send", "followup", "jobs") and not getattr(a, "dry_run", False):
                audit("RUN_START", "", f"command: {a.cmd}")
            try:
                code = {"run": cmd_run, "send": cmd_send, "followup": cmd_followup, "replies": cmd_replies,
                        "status": cmd_status, "report": cmd_report, "jobs": cmd_jobs}[a.cmd](a)
            except KeyboardInterrupt:
                audit("RUN_INTERRUPTED", "", "Stopped with Ctrl+C", "Safe to run again - nothing is sent twice")
                code = 130
            except Exception as e:  # noqa: BLE001
                with open(C.BASE / "error.log", "a", encoding="utf-8") as f:
                    f.write(f"\n[{C.iso()}] command={a.cmd}\n{traceback.format_exc()}")
                audit("CRASH", a.cmd, f"{type(e).__name__}: {e}"[:200],
                      "Send me error.log - safe to run again, nothing is sent twice")
                code = 1
            finally:
                if a.cmd not in ("status",) and not getattr(a, "dry_run", False):
                    try:
                        C.build_excel()
                    except Exception as e:  # noqa: BLE001
                        print(f"(Excel not updated: {e})")
            return code
    except C.Busy as e:
        print(e)
        return 0 if quiet else 1


if __name__ == "__main__":
    sys.exit(main())
