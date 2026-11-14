"""common.py - settings, database, audit log, contact loader, Excel tracker."""
import csv
import datetime as dt
import os
import re
import sqlite3
import time
from collections import Counter
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

BASE = Path(os.environ.get("OUTREACH_HOME") or Path(__file__).resolve().parent)
load_dotenv(BASE / ".env")
IST = ZoneInfo("Asia/Kolkata")


def env(key, default=""):
    return (os.environ.get(key) or default).strip()


# ------------------------------------------------------------------ settings
SENDER_NAME = "Revathi Sangam"
PHONE = "+91 8866004418"
GMAIL_USER = env("GMAIL_USER", "revathisangam2005@gmail.com")
GMAIL_APP_PASSWORD = env("GMAIL_APP_PASSWORD").replace(" ", "")
LINKEDIN = "https://www.linkedin.com/in/revathi-s-066727261/"
GITHUB = "https://github.com/Revathi-15"
RESUME = BASE / "REVATHI_SANGAM.pdf"

DAILY_LIMIT = int(env("DAILY_LIMIT", "60"))
PER_COMPANY_DAILY = int(env("PER_COMPANY_DAILY", "5"))
GAP_MIN = int(env("GAP_MIN", "60"))
GAP_MAX = int(env("GAP_MAX", "120"))
NOTIFY_EMAIL = env("NOTIFY_EMAIL", "true").lower() == "true"
# Mon-Fri, IST
WINDOWS = [(dt.time(10, 30), dt.time(12, 30)), (dt.time(14, 0), dt.time(16, 0))]

DB_PATH = BASE / "tracker.db"
AUDIT_CSV = BASE / "audit_log.csv"
EXCEL_OUT = BASE / "Job_Outreach_Tracker.xlsx"
TRACKER_PREFIX = "Job_Outreach_Tracker"


# ------------------------------------------------------------------ time
def now():
    return dt.datetime.now(IST)


def iso(t=None):
    return (t or now()).isoformat(timespec="seconds")


def parse_iso(s):
    return dt.datetime.fromisoformat(s)


def in_window(t=None):
    t = t or now()
    if t.weekday() >= 5:
        return False
    return any(s <= t.time() < e for s, e in WINDOWS)


def next_window_start(t=None):
    t = t or now()
    for off in range(8):
        d = t + dt.timedelta(days=off)
        if d.weekday() >= 5:
            continue
        for s, _ in WINDOWS:
            st = d.replace(hour=s.hour, minute=s.minute, second=0, microsecond=0)
            if st > t:
                return st
    return None


# ------------------------------------------------------------------ run lock
class Busy(Exception):
    pass


class RunLock:
    """Stops two runs of the same job from overlapping (would risk duplicates)."""

    def __init__(self, name):
        self.path = BASE / f"{name}.lock"

    def __enter__(self):
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return self
            except FileExistsError:
                if time.time() - self.path.stat().st_mtime >= 6 * 3600:
                    self.path.unlink()  # stale lock from a crashed run
                    continue
                raise Busy(f"Another '{self.path.stem}' run is in progress "
                           f"(delete {self.path.name} only if you are sure it is not).")
        raise Busy("Could not acquire lock")

    def __exit__(self, *exc):
        try:
            self.path.unlink()
        except OSError:
            pass


# ------------------------------------------------------------------ database
SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts(
  email TEXT PRIMARY KEY, company TEXT, first_name TEXT, source TEXT,
  status TEXT DEFAULT 'pending', note TEXT,
  subject TEXT, message_id TEXT, sent_at TEXT,
  followup_at TEXT, followup_message_id TEXT,
  replied INTEGER DEFAULT 0, reply_at TEXT, reply_type TEXT, reply_snippet TEXT,
  my_notes TEXT, added_at TEXT);
CREATE TABLE IF NOT EXISTS jobs(
  job_id TEXT PRIMARY KEY, company TEXT, title TEXT, location TEXT, url TEXT,
  url_status TEXT, fit TEXT, source TEXT, found_at TEXT, posted_at TEXT,
  notified INTEGER DEFAULT 0, referrals TEXT, alumni_link TEXT, recruiter_link TEXT,
  applied TEXT, notes TEXT);
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, action TEXT, target TEXT,
  details TEXT, user_action TEXT);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""
_conn = None


def db():
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, timeout=30)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(SCHEMA)
    return _conn


def meta_get(k):
    r = db().execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r["v"] if r else None


def meta_set(k, v):
    db().execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (k, v))
    db().commit()


# ------------------------------------------------------------------ audit log
def _safe(v):
    """Stop spreadsheet formula injection (cells starting with = + - @)."""
    if isinstance(v, str) and v and v[0] in "=+-@":
        return " " + v
    return v


def audit(action, target="", details="", user_action=""):
    ts = now().strftime("%Y-%m-%d %H:%M:%S")
    c = db()
    c.execute("INSERT INTO audit(ts,action,target,details,user_action) VALUES(?,?,?,?,?)",
              (ts, action, target, details, user_action))
    c.commit()
    new = not AUDIT_CSV.exists()
    try:
        with open(AUDIT_CSV, "w" if new else "a", newline="", encoding="utf-8-sig" if new else "utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["Timestamp (IST)", "Action", "Target", "Details", "Action needed from you"])
            w.writerow([_safe(x) for x in (ts, action, target, details, user_action)])
    except OSError:
        pass  # csv is open in Excel; the same entry is safely stored in tracker.db and the Audit Log sheet
    line = f"[{ts}] {action:<20} {target} {details}"
    if user_action:
        line += f"  --> YOU: {user_action}"
    print(line)


def audit_once_today(action, target="", details="", user_action=""):
    today = now().strftime("%Y-%m-%d")
    r = db().execute("SELECT 1 FROM audit WHERE action=? AND substr(ts,1,10)=?", (action, today)).fetchone()
    if not r:
        audit(action, target, details, user_action)


# ------------------------------------------------------------------ contacts from Excel
EMAIL_RX = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")
PERSONAL = {"gmail.com", "googlemail.com", "yahoo.com", "yahoo.in", "yahoo.co.in", "ymail.com", "icloud.com",
            "me.com", "hotmail.com", "outlook.com", "live.com", "msn.com", "rediffmail.com", "aol.com",
            "protonmail.com", "proton.me"}
ROLE_LOCAL = re.compile(r"^(no-?reply|do-?not-?reply|mailer-daemon|postmaster|abuse|privacy|unsubscribe)", re.I)
HEADER_STOP = {"hr", "researcher", "researchers", "talent", "email", "emails", "employee", "employees"}
NOT_NAMES = {"info", "hr", "careers", "career", "jobs", "contact", "support", "admin", "sales", "team", "hello",
             "talent", "recruiting", "recruitment", "recruiter", "mail", "help", "office", "press"}


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def domain_root(email):
    parts = email.split("@")[1].lower().split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net", "ac", "gov"):
        return parts[-3]
    return parts[-2] if len(parts) >= 2 else parts[0]


def pick_company(header, email):
    root = domain_root(email)
    h = _norm(header)
    if h and (root in h or (len(h) >= 3 and h in root)):
        return header.strip()
    if h:
        toks = re.findall(r"[a-z]+", header.lower())
        if not any(t in HEADER_STOP or t.startswith("recruit") for t in toks):
            return header.strip()
    return root.title()


def first_name(email):
    m = re.match(r"^([a-z]{3,})\.[a-z]+$", email.split("@")[0].lower())
    if m and m.group(1) not in NOT_NAMES:
        return m.group(1).title()
    return ""


def skip_reason(email):
    local, host = email.split("@")
    if host in PERSONAL:
        return "personal email domain (probably not a recruiter/employee)"
    if ROLE_LOCAL.match(local):
        return "no-reply / system mailbox"
    # never mail your own addresses
    if email.lower() in ("revathisangam2005@gmail.com", "sangamrevathi05@gmail.com"):
        return "own email address - skipped"
    return None


def _find_header(rows):
    for row in rows[:3]:
        low = [str(x).strip().lower() if x is not None else "" for x in row]
        if "company" in low:
            ci = low.index("company")
            if any("email" in x for x in low):
                return ci
    return None


def _header_above(rows, i, j):
    for k in range(i - 1, -1, -1):
        row = rows[k]
        v = row[j] if j < len(row) else None
        if isinstance(v, str) and v.strip() and not EMAIL_RX.search(v):
            return v.strip()
    return None


NAME_FIX = {"wellsfargo": "Wells Fargo", "brightmoney": "Bright Money", "angleone": "Angel One",
            "pinelabs": "Pine Labs", "moneyview": "Money View", "unacadamy": "Unacademy", "mykaarma": "myKaarma",
            "kukufm": "Kuku FM", "coinswitch": "CoinSwitch", "infoedge": "Info Edge", "bnymellon": "BNY Mellon"}


def clean_company(name):
    name = re.sub(r"\(.*?\)?$", "", name or "")  # "Clear( Fromarly ClearTax)" -> "Clear"
    name = re.sub(r"\s+[-\u2013]\s+.*$", "", name)  # "Amazon -  London" -> "Amazon"
    name = re.sub(r"\s+", " ", name).strip()
    return NAME_FIX.get(_norm(name), name)


def _canonicalize(out):
    """One clean company name per email domain (merges 'Fi'/'FI Money', 'Amazon - London' ...)."""
    for d in out.values():
        d["company"] = clean_company(d["company"])
    by_root = {}
    for em, d in out.items():
        if em.split("@")[1] not in PERSONAL:
            by_root.setdefault(domain_root(em), Counter())[d["company"]] += 1
    for em, d in out.items():
        root = domain_root(em)
        if root in by_root and em.split("@")[1] not in PERSONAL:
            best = by_root[root].most_common(1)[0][0]
            d["company"] = clean_company(best)


def load_contacts():
    """Scan every .xlsx in the folder (except our tracker) for email addresses."""
    from openpyxl import load_workbook
    import zipfile
    out = {}
    for f in sorted(BASE.glob("*.xlsx")):
        if f.name.startswith(("~$", TRACKER_PREFIX)):
            continue
        # Skip corrupted / non-xlsx files that have an .xlsx extension
        try:
            with zipfile.ZipFile(f, 'r'):
                pass
        except zipfile.BadZipFile:
            print(f"  WARNING: {f.name} is not a valid xlsx file (skipped)")
            continue
        try:
            wb = load_workbook(f, read_only=True, data_only=True)
        except Exception as e:
            print(f"  WARNING: could not open {f.name}: {e} (skipped)")
            continue
        for ws in wb.worksheets:
            rows = [list(r) for r in ws.iter_rows(values_only=True)]
            company_col = _find_header(rows)
            for i, row in enumerate(rows):
                for j, cell in enumerate(row):
                    if not isinstance(cell, str):
                        continue
                    for em in EMAIL_RX.findall(cell):
                        em = em.lower().strip(".")
                        if em in out:
                            continue
                        if company_col is not None:
                            raw = row[company_col] if company_col < len(row) else None
                            raw = raw if isinstance(raw, str) and not EMAIL_RX.search(raw) else None
                        else:
                            raw = _header_above(rows, i, j)
                        out[em] = dict(email=em, company=pick_company(raw, em), source=f"{f.name}:{ws.title}")
    _canonicalize(out)
    return out


def sync_contacts():
    """Add never-seen emails to the DB. Existing rows are never touched (no duplicates, ever)."""
    c = db()
    found = load_contacts()
    new = skipped = 0
    for em, d in found.items():
        if c.execute("SELECT 1 FROM contacts WHERE email=?", (em,)).fetchone():
            continue
        reason = skip_reason(em)
        c.execute("INSERT INTO contacts(email,company,first_name,source,status,note,added_at) VALUES(?,?,?,?,?,?,?)",
                  (em, d["company"], first_name(em), d["source"],
                   "skipped" if reason else "pending", reason, iso()))
        new += 1
        skipped += 1 if reason else 0
    c.commit()
    return len(found), new, skipped


# ------------------------------------------------------------------ Excel tracker
def pull_manual_edits():
    """Keep what the user typed in Excel (Applied?, Notes, My Notes) before we regenerate the file."""
    from openpyxl import load_workbook
    if not EXCEL_OUT.exists():
        return
    try:
        wb = load_workbook(EXCEL_OUT, data_only=True)
    except Exception:
        return
    c = db()
    plan = (("Jobs (Fresher)", "Job ID", "jobs", "job_id", {"Applied?": "applied", "Notes": "notes"}),
            ("Contacts & Mails", "Email", "contacts", "email", {"My Notes": "my_notes"}))
    for sheet, keyname, table, keycol, cols in plan:
        if sheet not in wb.sheetnames:
            continue
        ws = wb[sheet]
        hdr = [x.value for x in ws[1]]
        if keyname not in hdr:
            continue
        ki = hdr.index(keyname)
        for row in ws.iter_rows(min_row=2, values_only=True):
            key = row[ki]
            if not key:
                continue
            for col, field in cols.items():
                if col in hdr:
                    val = row[hdr.index(col)]
                    c.execute(f"UPDATE {table} SET {field}=? WHERE {keycol}=?", (val, key))
    c.commit()


def _delta(a, b):
    s = int((b - a).total_seconds())
    if s < 0:
        return ""
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m = s // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def _fmt(s):
    if not s:
        return ""
    try:
        return parse_iso(s).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return s


def build_excel():
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.datavalidation import DataValidation

    pull_manual_edits()
    c = db()
    q = lambda sql, *a: c.execute(sql, a).fetchone()[0]
    today = now().strftime("%Y-%m-%d")
    head_fill = PatternFill("solid", fgColor="1F4E78")
    green = PatternFill("solid", fgColor="E2EFDA")
    red = PatternFill("solid", fgColor="FCE4D6")
    yellow = PatternFill("solid", fgColor="FFF2CC")
    link_font = Font(color="0563C1", underline="single")
    WRAP = Alignment(wrap_text=True, vertical="top")

    def style_header(ws, row=1):
        for cell in ws[row]:
            if cell.value is not None:
                cell.fill = head_fill
                cell.font = Font(bold=True, color="FFFFFF")
                cell.alignment = Alignment(vertical="center", wrap_text=True)

    def wrap_all(ws, start_row=2):
        """Apply wrap_text to every data cell so long values are fully visible."""
        for row in ws.iter_rows(min_row=start_row):
            for cell in row:
                if cell.alignment:
                    cell.alignment = Alignment(
                        wrap_text=True,
                        vertical=cell.alignment.vertical or "top",
                        horizontal=cell.alignment.horizontal,
                    )
                else:
                    cell.alignment = WRAP

    def row_height(ws, start_row=2, default=45):
        for rd in range(start_row, ws.max_row + 1):
            ws.row_dimensions[rd].height = default

    def widths(ws, ws_widths):
        for i, w in enumerate(ws_widths, 1):
            ws.column_dimensions[get_column_letter(i)].width = w

    def make_links(ws, col_idx, text, first_row=2):
        for row in ws.iter_rows(min_row=first_row):
            cell = row[col_idx]
            if isinstance(cell.value, str) and cell.value.startswith("http"):
                cell.hyperlink = cell.value
                cell.value = text
                cell.font = link_font

    wb = Workbook()

    # ---------------- Summary
    ws = wb.active
    ws.title = "Summary"
    sent_total = q("SELECT COUNT(*) FROM contacts WHERE sent_at IS NOT NULL")
    replied = q("SELECT COUNT(*) FROM contacts WHERE replied=1")
    metrics = [
        ("Last updated (IST)", now().strftime("%Y-%m-%d %H:%M:%S")),
        ("Unique contacts loaded", q("SELECT COUNT(*) FROM contacts")),
        ("Mails sent (first mail)", sent_total),
        ("Mails sent today (incl. follow-ups)",
         q("SELECT COUNT(*) FROM contacts WHERE substr(sent_at,1,10)=?", today)
         + q("SELECT COUNT(*) FROM contacts WHERE substr(followup_at,1,10)=?", today)),
        ("Still pending (not mailed yet)", q("SELECT COUNT(*) FROM contacts WHERE status='pending'")),
        ("Follow-ups sent", q("SELECT COUNT(*) FROM contacts WHERE followup_at IS NOT NULL")),
        ("Replies received", replied),
        ("Reply rate", f"{(100 * replied / sent_total):.1f}%" if sent_total else "-"),
        ("Bounced / invalid address", q("SELECT COUNT(*) FROM contacts WHERE status='bounced'")),
        ("Failed", q("SELECT COUNT(*) FROM contacts WHERE status='failed'")),
        ("Uncertain (check Gmail Sent folder)", q("SELECT COUNT(*) FROM contacts WHERE status='uncertain'")),
        ("Skipped (personal / no-reply emails)", q("SELECT COUNT(*) FROM contacts WHERE status='skipped'")),
        ("Fresher jobs tracked", q("SELECT COUNT(*) FROM jobs")),
        ("Jobs not yet marked applied", q("SELECT COUNT(*) FROM jobs WHERE (applied IS NULL OR applied='')")),
    ]
    ws.append(["Metric", "Value"])
    for m in metrics:
        ws.append(list(m))
    style_header(ws)
    for r in range(2, ws.max_row + 1):
        ws.cell(r, 1).font = Font(bold=True)
    wrap_all(ws)

    ws.append([])
    ws.append(["REPLIES THAT NEED YOUR ACTION"])
    ws.cell(ws.max_row, 1).font = Font(bold=True, size=12)
    ws.append(["Company", "Email", "Reply type (auto-guess)", "Received (IST)", "What they wrote"])
    style_header(ws, ws.max_row)
    rows = c.execute("SELECT company,email,reply_type,reply_at,reply_snippet FROM contacts WHERE replied=1 "
                     "AND reply_type IN ('Positive - action needed','Reply - please read') "
                     "ORDER BY reply_at DESC").fetchall()
    for r in rows:
        ws.append([_safe(r[0]), r[1], r[2], _fmt(r[3]), _safe(r[4])])
        for cell in ws[ws.max_row]:
            cell.fill = green
    if not rows:
        ws.append(["(none yet)"])

    ws.append([])
    ws.append(["FRESHER JOBS TO APPLY (not marked applied yet, working links only)"])
    ws.cell(ws.max_row, 1).font = Font(bold=True, size=12)
    ws.append(["Company", "Role", "Location", "Apply link", "Job ID", "Fit"])
    style_header(ws, ws.max_row)
    jrows = c.execute("SELECT company,title,location,url,job_id,fit FROM jobs WHERE (applied IS NULL OR applied='') "
                      "AND url_status NOT LIKE 'Closed%' AND url_status NOT LIKE 'BROKEN%' "
                      "ORDER BY CASE WHEN fit LIKE 'Strong%' THEN 0 ELSE 1 END, found_at DESC LIMIT 25").fetchall()
    first_job_row = ws.max_row + 1
    for r in jrows:
        ws.append([_safe(r[0]), _safe(r[1]), r[2], r[3], r[4], r[5]])
    for row in ws.iter_rows(min_row=first_job_row):
        cell = row[3]
        if isinstance(cell.value, str) and cell.value.startswith("http"):
            cell.hyperlink = cell.value
            cell.value = "Open job"
            cell.font = link_font
    if not jrows:
        ws.append(["(none yet - run: python outreach.py jobs)"])
    widths(ws, [38, 34, 26, 22, 36, 70])

    # ---------------- Contacts & Mails
    ws = wb.create_sheet("Contacts & Mails")
    ws.append(["Company", "Email", "Name", "Status", "Subject", "Mail sent (IST)", "Follow-up sent (IST)",
               "Replied?", "Reply received (IST)", "Reply delay", "Reply type (auto-guess)",
               "What they wrote", "System note", "My Notes"])
    fills = []
    for r in c.execute("SELECT * FROM contacts ORDER BY (sent_at IS NULL), sent_at DESC, company, email"):
        delay = ""
        if r["replied"] and r["sent_at"] and r["reply_at"]:
            delay = _delta(parse_iso(r["sent_at"]), parse_iso(r["reply_at"]))
        ws.append([_safe(r["company"]), r["email"], r["first_name"], r["status"], _safe(r["subject"]),
                   _fmt(r["sent_at"]), _fmt(r["followup_at"]), "Yes" if r["replied"] else "No",
                   _fmt(r["reply_at"]), delay, r["reply_type"], _safe(r["reply_snippet"]),
                   _safe(r["note"]), r["my_notes"]])
        fills.append(green if r["replied"] else red if r["status"] in ("bounced", "failed", "uncertain") else None)
    style_header(ws)
    for i, f in enumerate(fills, 2):
        if f:
            for cell in ws[i]:
                cell.fill = f
    wrap_all(ws)
    row_height(ws)
    ws.freeze_panes = "C2"
    ws.auto_filter.ref = ws.dimensions
    widths(ws, [26, 36, 14, 11, 40, 20, 20, 9, 20, 11, 24, 60, 30, 30])

    # ---------------- Jobs Applied (tracks every job you apply to)
    ws = wb.create_sheet("Jobs Applied")
    ws.append(["Timestamp Applied (IST)", "Company", "Role", "Location", "Job Link (Apply URL)",
               "Job ID", "Source (ATS)", "Fit", "Link Status", "Notes", "Status"])
    for r in c.execute("SELECT * FROM jobs WHERE applied IS NOT NULL AND applied != '' "
                       "ORDER BY applied DESC"):
        ws.append([
            _fmt(r["applied"]) if r["applied"] not in ("Yes", "No", "Referral asked", "Skip") else _fmt(r["found_at"]),
            _safe(r["company"]),
            _safe(r["title"]),
            r["location"],
            r["url"],
            r["job_id"],
            r["source"],
            r["fit"],
            r["url_status"],
            r["notes"],
            r["applied"],
        ])
    style_header(ws)
    # make job link column clickable
    for row in ws.iter_rows(min_row=2):
        cell = row[4]
        if isinstance(cell.value, str) and cell.value.startswith("http"):
            cell.hyperlink = cell.value
            cell.value = cell.value  # keep full URL visible
            cell.font = link_font
    wrap_all(ws)
    row_height(ws)
    ws.freeze_panes = "B2"
    ws.auto_filter.ref = ws.dimensions
    widths(ws, [22, 24, 44, 24, 60, 38, 20, 20, 18, 30, 14])

    # ---------------- Jobs
    ws = wb.create_sheet("Jobs (Fresher)")
    ws.append(["Company", "Role", "Location", "Job Link (Full URL)", "Link check", "Fit",
               "Job ID", "Found (IST)", "Posted",
               "People at this company from YOUR email list",
               "LinkedIn: NIT Durgapur alumni there",
               "LinkedIn: recruiters there", "Applied?", "Notes"])
    for r in c.execute("SELECT * FROM jobs ORDER BY CASE WHEN fit LIKE 'Strong%' THEN 0 ELSE 1 END, found_at DESC"):
        ws.append([_safe(r["company"]), _safe(r["title"]), r["location"], r["url"], r["url_status"], r["fit"],
                   r["job_id"], _fmt(r["found_at"]), r["posted_at"] or "",
                   r["referrals"], r["alumni_link"], r["recruiter_link"],
                   r["applied"], r["notes"]])
    style_header(ws)
    # col 3 (index 3) = Job Link
    for row in ws.iter_rows(min_row=2):
        cell = row[3]
        if isinstance(cell.value, str) and cell.value.startswith("http"):
            cell.hyperlink = cell.value
            cell.value = cell.value  # keep full URL so Job ID matching works
            cell.font = link_font
    make_links(ws, 10, "Search alumni")
    make_links(ws, 11, "Search recruiters")
    wrap_all(ws)
    row_height(ws)
    ws.freeze_panes = "C2"
    ws.auto_filter.ref = ws.dimensions
    dv = DataValidation(type="list", formula1='"Yes,No,Referral asked,Skip"', allow_blank=True)
    ws.add_data_validation(dv)
    dv.add(f"M2:M{max(ws.max_row, 2) + 500}")
    widths(ws, [24, 44, 24, 60, 16, 20, 36, 20, 12, 46, 22, 22, 14, 30])

    # ---------------- Audit log
    ws = wb.create_sheet("Audit Log")
    ws.append(["Timestamp (IST)", "Action", "Target", "Details", "Action needed from you"])
    for r in c.execute("SELECT ts,action,target,details,user_action FROM audit ORDER BY id DESC LIMIT 5000"):
        ws.append([_safe(x) for x in tuple(r)])
        if r["user_action"]:
            for cell in ws[ws.max_row]:
                cell.fill = yellow
    style_header(ws)
    wrap_all(ws)
    row_height(ws, default=35)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    widths(ws, [22, 22, 40, 80, 60])

    tmp = EXCEL_OUT.with_name("_tracker_tmp.xlsx")
    wb.save(tmp)
    try:
        os.replace(tmp, EXCEL_OUT)
        return EXCEL_OUT
    except PermissionError:
        alt = EXCEL_OUT.with_name(f"{TRACKER_PREFIX}_{now():%Y%m%d_%H%M}.xlsx")
        os.replace(tmp, alt)
        print(f"NOTE: {EXCEL_OUT.name} is open in Excel, saved as {alt.name} instead. Close Excel to avoid this.")
        return alt
