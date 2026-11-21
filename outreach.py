#!/usr/bin/env python3
"""Revathi's job-outreach automation.

  python outreach.py replies   # check Gmail inbox for replies/bounces
  python outreach.py status    # quick counts
  python outreach.py report    # rebuild Excel tracker
  python outreach.py jobs      # find fresher openings
  python outreach.py followup  # send follow-ups (5+ days, no reply)
"""
import datetime as dt
import argparse
import email
import email.policy
import html
import imaplib
import re
import smtplib
import ssl
import sys
import time
import os
import traceback
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr, parsedate_to_datetime

# pythonw (no console) needs a sink for stdout/stderr
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

import common as C
from common import audit, audit_once_today

# ------------------------------------------------------------------ email content
SUBJECT = ("Application for Software Development Engineer - Revathi Sangam | "
           "Amazon & Publicis Sapient Intern")

P_INTRO  = ("I'm Revathi Sangam, a 2026 B.Tech CSE graduate from NIT Durgapur (CGPA: 8.91), "
            "with internship experience at Amazon and Publicis Sapient.")
P_EXP    = ("I have experience across the SDLC, including backend development, API design, testing, "
            "debugging, production issue resolution, design documentation and cross-functional collaboration. "
            "At Amazon, I built a self-service portal that reduced configuration turnaround from 5\u20138 days "
            "to 5 minutes and resolved critical production issues impacting 10+ vendors. At Publicis Sapient, "
            "I developed an AI agent-based application that reduced manual query effort by 87%.")
P_CTA    = ("I'm exploring Software Development Engineer opportunities at {company} and would appreciate "
            "your consideration for suitable openings or a referral to the relevant hiring manager.")
P_ATTACH = "Resume attached for your reference.\n\n[[LI]] | [[GH]]"
P_THANKS = ""
SIGN     = f"Regards,\n{C.SENDER_NAME}\n{C.PHONE}"

F_1   = ("I hope you're having a productive week! I'm following up on my earlier note regarding SDE "
         "opportunities at {company}.")
F_2   = ("I remain very enthusiastic about the chance to contribute to your team. If you have a few minutes "
         "for a quick chat, I would welcome the opportunity.")
F_3   = "Thanks again for your time!"
SIGN_F = f"{C.SENDER_NAME}\n{C.PHONE} | {C.GMAIL_USER}"


def render(company, name, followup=False):
    greet = f"Hi {name}," if name else "Hi,"
    if followup:
        paras = [greet, F_1.format(company=company), F_2, F_3, SIGN_F]
    else:
        # Build body paragraphs; skip empty strings (P_THANKS is "")
        paras = [p for p in [greet, P_INTRO, P_EXP, P_CTA.format(company=company),
                              P_ATTACH, SIGN] if p]
    plain = "\n\n".join(paras)
    plain = (plain
             .replace("[[LI]]", f"LinkedIn: {C.LINKEDIN}")
             .replace("[[GH]]", f"GitHub: {C.GITHUB}"))
    h = "".join("<p>" + html.escape(p).replace("\n", "<br>") + "</p>" for p in paras)
    h = (h.replace("[[LI]]", f'<a href="{C.LINKEDIN}">LinkedIn</a>')
           .replace("[[GH]]", f'<a href="{C.GITHUB}">GitHub</a>'))
    return plain, h


def build_msg(to, subject, plain, html_body, reply_to=None, attach=True):
    m = EmailMessage()
    m["From"]       = f"{C.SENDER_NAME} <{C.GMAIL_USER}>"
    m["To"]         = to
    m["Subject"]    = subject
    m["Date"]       = formatdate(localtime=True)
    mid             = make_msgid(domain="gmail.com")
    m["Message-ID"] = mid
    if reply_to:
        m["In-Reply-To"] = reply_to
        m["References"]  = reply_to
    m.set_content(plain)
    m.add_alternative(html_body, subtype="html")
    if attach:
        m.add_attachment(C.RESUME.read_bytes(), maintype="application",
                         subtype="pdf", filename=C.RESUME.name)
    return m, mid


class Mailer:
    """Fresh SMTP connection per message — no reuse, no timeout duplicates."""

    def send(self, msg):
        ctx = ssl.create_default_context()
        s = smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx, timeout=30)
        s.login(C.GMAIL_USER, C.GMAIL_APP_PASSWORD)
        refused = s.send_message(msg)   # returns {} on success, raises on error
        try:
            s.quit()
        except Exception:
            pass
        if refused:
            raise smtplib.SMTPRecipientsRefused(refused)

    def close(self):
        pass   # nothing to close — connections are per-send


def classify_error(e):
    """Return (outcome, stop_run, message). outcome is 'failed' or 'retry'."""
    if isinstance(e, smtplib.SMTPAuthenticationError):
        return "retry", True, "Gmail login failed - check GMAIL_APP_PASSWORD in .env"
    if isinstance(e, smtplib.SMTPRecipientsRefused):
        return "failed", False, str(e)[:200]
    if isinstance(e, smtplib.SMTPDataError):
        t = str(e).lower()
        if "quota" in t or "limit" in t or "5.4.5" in t:
            return "retry", True, "Gmail sending limit reached"
        return "failed", False, str(e)[:200]
    return "retry", True, f"Network/SMTP problem: {e}"[:200]


# ------------------------------------------------------------------ follow-up
def cmd_followup(a):
    c = C.db()
    if not a.dry_run:
        try:
            do_replies(c, force=True)   # never follow up on someone who already replied
        except Exception as e:
            audit("FOLLOWUP_ABORTED", "", f"Could not check replies first ({e})",
                  "Fix IMAP access and retry")
            return 1
    cutoff = (C.now() - dt.timedelta(days=a.min_days)).isoformat(timespec="seconds")
    rows = c.execute(
        "SELECT * FROM contacts WHERE status='sent' AND replied=0 "
        "AND followup_at IS NULL AND followup_message_id IS NULL "
        "AND sent_at <= ? ORDER BY sent_at", (cutoff,)
    ).fetchall()
    if a.dry_run:
        print(f"DRY RUN - {len(rows)} contacts eligible for follow-up ({a.min_days}+ days ago, no reply).")
        for r in rows[:3]:
            plain, _ = render(r["company"], r["first_name"], True)
            print(f"\n{'='*70}\nTO: {r['email']}\nSUBJECT: Re: {r['subject']}\n\n{plain}")
        return 0
    if not rows:
        audit("NOTHING_TO_SEND", "", f"No follow-ups due (need {a.min_days}+ days since first mail, no reply)")
        return 0
    audit("FOLLOWUP_START", "", f"{len(rows)} eligible contacts")
    import random
    mailer = Mailer()
    sent = 0
    for r in rows:
        plain, h = render(r["company"], r["first_name"], followup=True)
        subject = "Re: " + r["subject"]
        msg, mid = build_msg(r["email"], subject, plain, h,
                             reply_to=r["message_id"], attach=False)
        c.execute("UPDATE contacts SET followup_message_id=? WHERE email=?", (mid, r["email"]))
        c.commit()
        try:
            mailer.send(msg)
            stamp = C.iso()
            c.execute("UPDATE contacts SET followup_at=? WHERE email=?", (stamp, r["email"]))
            c.commit()
            audit("FOLLOWUP_SENT", r["email"], f"{r['company']}: {subject}")
            sent += 1
            if sent < len(rows):
                time.sleep(random.randint(60, 90))
        except Exception as e:
            _, _, text = classify_error(e)
            c.execute("UPDATE contacts SET note=? WHERE email=?",
                      ("Follow-up failed: " + text, r["email"]))
            c.commit()
            audit("FAILED", r["email"], f"{r['company']}: {text}")
    audit("FOLLOWUP_SUMMARY", "", f"{sent} follow-ups sent, {len(rows)-sent} still eligible")
    return 0


# ------------------------------------------------------------------ replies (IMAP)
MONTHS   = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
AUTO_KEYS = ["out of office","automatic reply","auto-reply","autoreply","auto reply","on leave",
             "away from","vacation","this is an automated","do not reply to this"]
NEG_KEYS  = ["not hiring","no openings","no open positions","no current opening","unfortunately","regret",
             "not looking","hiring freeze","not currently hiring","no vacancies"]
POS_KEYS  = ["interested","resume","schedule","call","connect","forward","refer","apply","opening",
             "position","happy to","share","next steps","discuss"]

ACTIONS = {
    "Positive - action needed":    "Reply to {e} soon - the response looks positive",
    "Reply - please read":         "Read and reply to {e}",
    "Negative / no openings":      "No action (they said no openings)",
    "Auto-reply (out of office)":  "No action - automatic reply",
}


def body_text(msg):
    try:
        part = msg.get_body(preferencelist=("plain", "html"))
        if part is None:
            return ""
        txt = part.get_content()
        if part.get_content_type() == "text/html":
            txt = html.unescape(re.sub(r"<[^>]+>", " ", txt))
        return txt
    except Exception:
        return ""


def clean_snippet(txt):
    lines = txt.splitlines()
    keep  = []
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s.startswith(">"):
            continue
        nxt = lines[i+1] if i+1 < len(lines) else ""
        if (s.startswith("On ") and ("wrote:" in s or "wrote:" in nxt)) or \
                re.match(r"^(From:|-----Original Message|Sent from my)", s, re.I):
            break
        keep.append(s)
    return re.sub(r"\s+", " ", " ".join(keep)).strip()[:300]


def classify_reply(msg, text):
    low  = (text + " " + str(msg.get("Subject", ""))).lower()
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


def process_incoming(c, msg):
    frm = parseaddr(str(msg.get("From", "")))[1].lower()
    if not frm or frm == C.GMAIL_USER.lower():
        return None

    # bounce detection
    if "mailer-daemon" in frm or "postmaster" in frm:
        blob = ""
        for part in msg.walk():
            if part.get_content_maintype() == "text":
                try:
                    blob += part.get_content()
                except Exception:
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
                audit("BOUNCE_DETECTED", em, f"{r['company']}: rejected by their server",
                      "Invalid address - will not be retried")
                hit = "bounce"
        return hit

    # match to a sent contact via message-id references
    refs = set((str(msg.get("In-Reply-To","")) + " " + str(msg.get("References",""))).split())
    row  = None
    for ref in refs:
        row = c.execute("SELECT * FROM contacts WHERE message_id=? OR followup_message_id=?",
                        (ref, ref)).fetchone()
        if row:
            break
    try:
        when = parsedate_to_datetime(str(msg.get("Date"))).astimezone(C.IST)
    except Exception:
        when = C.now()
    if not row:
        row = c.execute("SELECT * FROM contacts WHERE email=? AND sent_at IS NOT NULL", (frm,)).fetchone()
        if row and when < C.parse_iso(row["sent_at"]):
            row = None
    if not row:
        return None

    text  = body_text(msg)
    rtype = classify_reply(msg, text)
    if row["replied"] and not str(row["reply_type"]).startswith("Auto-reply"):
        return None
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
    ours  = set()
    for r in c.execute("SELECT message_id, followup_message_id FROM contacts"):
        ours.update(x for x in r if x)
    found = 0
    imap  = imaplib.IMAP4_SSL("imap.gmail.com", 993)
    try:
        imap.login(C.GMAIL_USER, C.GMAIL_APP_PASSWORD)
        imap.select("INBOX", readonly=True)
        _, data = imap.uid("SEARCH", None, "SINCE",
                           f"{since.day:02d}-{MONTHS[since.month-1]}-{since.year}")
        uids   = data[0].split()
        wanted = []
        for i in range(0, len(uids), 200):
            chunk = ",".join(u.decode() for u in uids[i:i+200])
            _, resp = imap.uid("FETCH", chunk,
                               "(UID BODY.PEEK[HEADER.FIELDS (FROM IN-REPLY-TO REFERENCES)])")
            for item in resp:
                if isinstance(item, tuple):
                    m   = re.search(rb"UID (\d+)", item[0])
                    uid = m.group(1) if m else None
                    hdr = email.message_from_bytes(item[1])
                    frm = parseaddr(str(hdr.get("From","")))[1].lower()
                    refs = set((str(hdr.get("In-Reply-To","")) + " " + str(hdr.get("References",""))).split())
                    if uid and (frm in known or "mailer-daemon" in frm
                                or "postmaster" in frm or refs & ours):
                        wanted.append(uid)
        for uid in wanted:
            _, resp = imap.uid("FETCH", uid.decode(), "(BODY.PEEK[])")
            raw = next((x[1] for x in resp if isinstance(x, tuple)), None)
            if raw and process_incoming(
                    c, email.message_from_bytes(raw, policy=email.policy.default)):
                found += 1
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    C.meta_set("last_reply_check", C.iso())
    return found


def cmd_mark_invalid(a):
    """Mark one or more emails as 'invalid' (person left / address defunct)."""
    c = C.db()
    marked = 0
    for em in a.emails:
        em = em.strip().lower()
        row = c.execute("SELECT status FROM contacts WHERE email=?", (em,)).fetchone()
        if not row:
            print(f"  NOT FOUND in DB: {em}")
            continue
        reason = a.reason or "Marked invalid by user (person left / email defunct)"
        c.execute("UPDATE contacts SET status='invalid', note=? WHERE email=?", (reason, em))
        c.commit()
        audit("MARKED_INVALID", em, reason)
        print(f"  MARKED INVALID: {em}  ({reason})")
        marked += 1
    if marked:
        C.build_excel()
        print(f"\n{marked} email(s) marked as 'invalid'. Excel updated.")
    return 0


def cmd_mark_bounced_as_invalid(a):
    """Optionally re-label all 'bounced' rows as 'invalid' in one shot."""
    c = C.db()
    rows = c.execute("SELECT email FROM contacts WHERE status='bounced'").fetchall()
    for r in rows:
        c.execute("UPDATE contacts SET status='invalid', note='Auto-reclassified from bounced: address not deliverable / person left' WHERE email=?",
                  (r["email"],))
    c.commit()
    audit("RECLASSIFY", "", f"{len(rows)} bounced contacts reclassified as invalid")
    C.build_excel()
    print(f"{len(rows)} 'bounced' contacts reclassified as 'invalid'. Excel updated.")
    return 0


    c = C.db()
    try:
        n = do_replies(c, force=True)
    except Exception as e:
        audit("REPLY_CHECK_FAILED", "", str(e)[:200],
              "Check GMAIL_APP_PASSWORD and that IMAP is enabled in Gmail settings")
        return 1
    audit("REPLY_CHECK", "", f"Inbox checked, {n} new replies/bounces recorded")
    return 0


def cmd_status(a):
    c = C.db()
    print("Contacts by status:")
    for r in c.execute("SELECT status, COUNT(*) n FROM contacts GROUP BY status ORDER BY n DESC"):
        print(f"  {r['status']:<12} {r['n']}")
    print(f"  replied      {c.execute('SELECT COUNT(*) FROM contacts WHERE replied=1').fetchone()[0]}")
    print(f"Jobs tracked: {c.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]}")
    return 0


def cmd_report(a):
    print(f"Excel updated: {C.build_excel()}")
    return 0


def cmd_jobs(a):
    import jobs
    return jobs.run_jobs(auto=getattr(a, "auto", False), notify=not a.no_notify)


def cmd_boards(a):
    """Search LinkedIn, Indeed, Glassdoor, Naukri for fresher SDE jobs posted in last N hours."""
    import job_boards
    return job_boards.run(hours_old=a.hours, dry_run=a.dry_run)


# ------------------------------------------------------------------ main
def main(argv=None):
    p   = argparse.ArgumentParser(description="Job outreach automation")
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("followup", help="send follow-ups to people who didn't reply after 5+ days")
    f.add_argument("--min-days", type=int, default=5)
    f.add_argument("--dry-run",  action="store_true")

    mi = sub.add_parser("mark-invalid",
                        help="mark one or more emails as invalid (person left / address defunct)")
    mi.add_argument("emails", nargs="+", help="email address(es) to mark")
    mi.add_argument("--reason", default="", help="optional note explaining why")

    sub.add_parser("reclassify-bounced",
                   help="re-label all 'bounced' rows as 'invalid' in one shot")

    sub.add_parser("replies", help="check Gmail inbox for replies and bounces")
    sub.add_parser("status",  help="quick counts")
    sub.add_parser("report",  help="rebuild the Excel tracker")

    b = sub.add_parser("boards",
                        help="search LinkedIn/Indeed/Glassdoor/Naukri for fresher SDE jobs posted in last 24h")
    b.add_argument("--hours",   type=int, default=24,  help="hours window (default 24)")
    b.add_argument("--dry-run", action="store_true",   help="print only, don't email or save")

    j = sub.add_parser("jobs", help="find fresher openings from company ATS boards and notify")
    j.add_argument("--auto",      action="store_true")
    j.add_argument("--no-notify", action="store_true")

    a = p.parse_args(argv)
    try:
        code = {"followup": cmd_followup, "replies": cmd_replies,
                "status": cmd_status, "report": cmd_report, "jobs": cmd_jobs,
                "boards": cmd_boards,
                "mark-invalid": cmd_mark_invalid,
                "reclassify-bounced": cmd_mark_bounced_as_invalid}[a.cmd](a)
        if a.cmd != "status":
            try:
                C.build_excel()
            except Exception as e:
                print(f"(Excel not updated: {e})")
        return code
    except KeyboardInterrupt:
        audit("RUN_INTERRUPTED", "", f"Stopped with Ctrl+C ({a.cmd})")
        return 130
    except Exception as e:
        with open(C.BASE / "error.log", "a", encoding="utf-8") as f:
            f.write(f"\n[{C.iso()}] command={a.cmd}\n{traceback.format_exc()}")
        audit("CRASH", a.cmd, f"{type(e).__name__}: {e}"[:200])
        return 1


if __name__ == "__main__":
    sys.exit(main())
