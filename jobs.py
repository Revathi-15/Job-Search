"""jobs.py - finds fresher-friendly openings on public job-board APIs and notifies you.

Uses only the official public JSON endpoints of Greenhouse, Lever and Ashby (no scraping, no LinkedIn).
Companies on other career sites (Amazon, Walmart, Oracle ...) are not covered - see README.
"""
import csv
import json
import platform
import re
import smtplib
import ssl
import subprocess
import time
from collections import Counter
from email.message import EmailMessage
from urllib.parse import quote

import requests

import common as C
from common import audit

CACHE = C.BASE / "ats_cache.json"
WATCHLIST = C.BASE / "watchlist.csv"  # optional: company,ats,slug
HEADERS = {"User-Agent": "Mozilla/5.0 (personal job-watch script)"}
JOBSUIT_URL = "https://dashboard.jobsuit.ai/joblist"  # open this to log applied jobs manually

TECH = re.compile(r"\b(software|sde|swe|developer|engineer(ing)?|backend|back-end|full[ -]?stack|front[ -]?end|"
                  r"devops|sre|machine learning|ml|ai|member of technical staff|mts)\b", re.I)
NON_TECH = re.compile(r"\b(sales|solutions?|support|customer|mechanical|electrical|civil|manufacturing|hardware|"
                      r"field|recruiter|marketing|finance|legal|account|technician)\b", re.I)
SENIOR = re.compile(r"\b(senior|sr\.?|staff|principal|lead|manager|director|architect|head|vp|distinguished|"
                    r"fellow|ii|iii|iv|2|3|4|5|l[3-9]|e[4-9])\b", re.I)
STRONG = re.compile(r"\b(sde[ -]?(1|i)|software (development )?engineer[ -]?(1|i)|engineer[ -]?(1|i)|"
                    r"new[ -]grad(uate)?|graduate|entry[ -]level|fresher|junior|associate|trainee|"
                    r"early career|campus|university|apprentice|"
                    r"2026\s*(batch|passout|pass.?out|graduate)?|2025\s*(batch|passout)?|2027\s*(batch|passout)?)\b", re.I)
INTERN = re.compile(r"\b(intern|internship)\b", re.I)
INDIA = ["india", "bengaluru", "bangalore", "hyderabad", "pune", "chennai", "mumbai", "gurgaon", "gurugram",
         "noida", "delhi", "kolkata", "ahmedabad", "kochi", "coimbatore", "jaipur", "indore"]
INDIA += [x.strip().lower() for x in C.env("EXTRA_LOCATIONS").split(",") if x.strip()]


MIN_SALARY_LPA = float(C.env("MIN_SALARY_LPA", "18"))   # skip jobs below this (when salary is listed)


def salary_lpa(text):
    """Extract the max LPA figure from a job title / description string. Returns None if not found."""
    if not text:
        return None
    # patterns: "18 LPA", "18-24 LPA", "₹18L", "INR 18,00,000", "1800000"
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:[-–]\s*\d+(?:\.\d+)?)?\s*(?:lpa|l\.p\.a|lakhs?/yr|lac)", text, re.I)
    if m:
        return float(m.group(1))
    m = re.search(r"(?:inr|₹|rs\.?)\s*(\d[\d,]*)", text, re.I)
    if m:
        v = float(m.group(1).replace(",", ""))
        return round(v / 100000, 1)
    return None



    """Returns fit label, or None if not relevant for a fresher software role."""
    if not TECH.search(title) or NON_TECH.search(title):
        return None
    if SENIOR.search(title):
        return None
    if INTERN.search(title) and not re.search(r"graduate|new grad|full[ -]?time", title, re.I):
        return "Internship"
    if STRONG.search(title):
        return "Strong (entry-level)"
    return "Possible (check experience needed)"


def in_india(loc):
    loc = (loc or "").lower()
    return any(k in loc for k in INDIA)


# ------------------------------------------------------------------ job-board APIs
def fetch_board(s, ats, slug):
    """Return list of job dicts, or None if no such board. Raises requests exceptions on network errors."""
    if ats == "greenhouse":
        r = s.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", timeout=15)
        if r.status_code != 200:
            return None
        return [dict(id=str(j["id"]), title=j.get("title", ""), location=(j.get("location") or {}).get("name", ""),
                     url=j.get("absolute_url", ""), posted=j.get("updated_at", ""))
                for j in r.json().get("jobs", [])]
    if ats == "lever":
        r = s.get(f"https://api.lever.co/v0/postings/{slug}?mode=json", timeout=15)
        if r.status_code != 200 or not isinstance(r.json(), list):
            return None
        out = []
        for j in r.json():
            cat = j.get("categories") or {}
            loc = " / ".join([cat.get("location") or ""] + list(cat.get("allLocations") or []))
            created = j.get("createdAt")
            out.append(dict(id=str(j["id"]), title=j.get("text", ""), location=loc, url=j.get("hostedUrl", ""),
                            posted=time.strftime("%Y-%m-%d", time.gmtime(created / 1000)) if created else ""))
        return out
    if ats == "ashby":
        r = s.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", timeout=15)
        if r.status_code != 200:
            return None
        data = r.json()
        if not isinstance(data, dict) or "jobs" not in data:
            return None
        return [dict(id=str(j["id"]), title=j.get("title", ""),
                     location=" / ".join([j.get("location") or ""] + [x.get("location", "") if isinstance(x, dict) else str(x)
                                                                      for x in (j.get("secondaryLocations") or [])]),
                     url=j.get("jobUrl", ""), posted=str(j.get("publishedAt") or j.get("publishedDate") or ""))
                for j in data["jobs"] if j.get("isListed", True)]
    return None


def gh_name_ok(s, slug, company, roots):
    try:
        r = s.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}", timeout=10)
        nn = C._norm(r.json().get("name", ""))
    except Exception:  # noqa: BLE001
        return True
    return bool(nn) and any(x and (x in nn or nn in x) for x in [C._norm(company)] + roots)


def slug_candidates(company, roots):
    base = company.lower().strip()
    cands = [re.sub(r"[^a-z0-9]", "", base), re.sub(r"[^a-z0-9]+", "-", base).strip("-")] + list(roots)
    out = []
    for x in cands:
        if x and x not in out:
            out.append(x)
    return out[:4]


def discover(s, company, roots, cache):
    key = C._norm(company)
    ent = cache.get(key)
    if ent is not None and (ent.get("ats") or time.time() - ent.get("ts", 0) < 30 * 86400):
        return ent
    err = False
    for slug in slug_candidates(company, roots):
        for ats in ("greenhouse", "lever", "ashby"):
            try:
                jobs = fetch_board(s, ats, slug)
            except (requests.RequestException, ValueError):
                err = True
                continue
            if jobs is None:
                continue
            if ats == "greenhouse" and not gh_name_ok(s, slug, company, roots):
                continue
            cache[key] = {"ats": ats, "slug": slug, "ts": time.time()}
            return cache[key]
    if not err:
        cache[key] = {"ats": None, "ts": time.time()}  # remember "not on these boards" for 30 days
    return cache.get(key, {"ats": None})


def check_link(s, url):
    if not url:
        return "BROKEN (no link)"
    try:
        r = s.get(url, timeout=12, allow_redirects=True, stream=True)
        code = r.status_code
        r.close()
    except requests.RequestException as e:
        return f"BROKEN ({type(e).__name__})"
    if code in (403, 429, 999):
        return f"UNVERIFIED ({code}, site blocks bots - open it manually)"
    return f"OK ({code})" if code < 400 else f"BROKEN ({code})"


# ------------------------------------------------------------------ notifications
QUIET = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
if platform.system() == "Windows":
    QUIET["creationflags"] = 0x08000000  # CREATE_NO_WINDOW


def desktop_notify(title, message):
    system = platform.system()
    try:
        if system == "Darwin":
            subprocess.run(["osascript", "-e", f"display notification {json.dumps(message)} with title {json.dumps(title)}"],
                           timeout=10, **QUIET)
        elif system == "Windows":
            t, m = title.replace("'", "''"), message.replace("'", "''")
            ps = ("Add-Type -AssemblyName System.Windows.Forms; Add-Type -AssemblyName System.Drawing; "
                  "$n = New-Object System.Windows.Forms.NotifyIcon; $n.Icon=[System.Drawing.SystemIcons]::Information; "
                  f"$n.Visible=$true; $n.ShowBalloonTip(10000,'{t}','{m}',[System.Windows.Forms.ToolTipIcon]::Info); "
                  "Start-Sleep -Seconds 8; $n.Dispose()")
            subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps], **QUIET)
        else:
            subprocess.run(["notify-send", title, message], timeout=10, **QUIET)
        return True
    except Exception:  # noqa: BLE001
        return False


def email_self(subject, body, jobs_list=None):
    if not (C.NOTIFY_EMAIL and C.GMAIL_APP_PASSWORD):
        return False
    m = EmailMessage()
    m["From"] = f"Job Watcher <{C.GMAIL_USER}>"
    m["To"] = C.GMAIL_USER
    m["Subject"] = subject
    m.set_content(body)
    # Build rich HTML version when job list is provided
    if jobs_list:
        rows_html = ""
        for j in jobs_list:
            fit_color = "#2e7d32" if "Strong" in (j.get("fit") or "") else "#e65100"
            salary_note = ""
            lpa = salary_lpa(j.get("title", "") + " " + (j.get("notes") or ""))
            if lpa:
                salary_note = f"<br><small>Salary hint: ~{lpa} LPA</small>"
            rows_html += f"""
<tr>
  <td style='padding:10px;border-bottom:1px solid #eee;'>
    <strong>{j['company']}</strong><br>
    <span style='color:{fit_color};font-weight:bold;'>{j['fit']}</span>
    {salary_note}
  </td>
  <td style='padding:10px;border-bottom:1px solid #eee;'>{j['title']}</td>
  <td style='padding:10px;border-bottom:1px solid #eee;'>{j['location']}</td>
  <td style='padding:10px;border-bottom:1px solid #eee;'>
    <a href='{j['url']}' style='background:#1565c0;color:#fff;padding:6px 14px;
       border-radius:4px;text-decoration:none;font-weight:bold;'>APPLY NOW</a><br>
    <small style='color:#666;'>{j['url_status']}</small>
  </td>
  <td style='padding:10px;border-bottom:1px solid #eee;font-size:12px;'>
    <a href='{j['alumni_link']}'>NIT DGP alumni</a><br>
    <a href='{j['recruiter_link']}'>Recruiters</a>
    {"<br><em>Contacts: " + (j['referrals'] or '') + "</em>" if j.get('referrals') else ''}
  </td>
</tr>"""
        html_body = f"""
<html><body style='font-family:Arial,sans-serif;max-width:900px;margin:auto;'>
<h2 style='color:#1565c0;'>🎯 New Fresher Job Openings (≥{MIN_SALARY_LPA} LPA or unspecified)</h2>
<p>Found <strong>{len(jobs_list)}</strong> matching opening(s). Apply to <em>Strong</em> ones first.</p>
<table style='width:100%;border-collapse:collapse;'>
  <tr style='background:#1565c0;color:#fff;'>
    <th style='padding:10px;text-align:left;'>Company</th>
    <th style='padding:10px;text-align:left;'>Role</th>
    <th style='padding:10px;text-align:left;'>Location</th>
    <th style='padding:10px;text-align:left;'>Apply</th>
    <th style='padding:10px;text-align:left;'>Resources</th>
  </tr>
  {rows_html}
</table>
<hr>
<p style='font-size:12px;color:#888;'>
  Auto-sent by your job-search script • 
  Job ID format: <code>ats:slug:id</code> (use in <code>python outreach.py jobs</code>) •
  Open <strong>Job_Outreach_Tracker.xlsx → Jobs (Fresher)</strong> for full details.
</p>
</body></html>"""
        m.add_alternative(html_body, subtype="html")
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=60) as s:
            s.login(C.GMAIL_USER, C.GMAIL_APP_PASSWORD)
            s.send_message(m)
        return True
    except Exception as e:  # noqa: BLE001
        print(f"(notification email failed: {e})")
        return False


def describe(j):
    return (f"{j['title']} @ {j['company']} ({j['location']}) [{j['fit']}]\n"
            f"  Apply: {j['url']}   (link check: {j['url_status']})\n"
            f"  People at {j['company']} in your email list: {j['referrals'] or 'none'}\n"
            f"  NIT Durgapur alumni there (LinkedIn search): {j['alumni_link']}\n"
            f"  Recruiters there (LinkedIn search): {j['recruiter_link']}\n")


# ------------------------------------------------------------------ main
def load_watchlist():
    out = {}
    if WATCHLIST.exists():
        with open(WATCHLIST, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                co, ats, slug = (row.get("company") or "").strip(), (row.get("ats") or "").strip().lower(), \
                                (row.get("slug") or "").strip()
                if co and ats in ("greenhouse", "lever", "ashby") and slug:
                    out[C._norm(co)] = {"company": co, "ats": ats, "slug": slug}
    return out


def apply_job(job_id, apply_url=None, notes=None):
    """Mark a job as applied. Records exact IST timestamp.
    Prints a reminder to also log it on Jobsuit dashboard.
    """
    c = C.db()
    row = c.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    if not row:
        print(f"Job ID not found: {job_id}")
        return False
    stamp = C.iso()           # exact IST timestamp e.g. 2026-11-13T16:30:00+05:30
    note_parts = []
    if notes:
        note_parts.append(notes)
    if apply_url:
        note_parts.append(f"Applied via: {apply_url}")
    full_note = " | ".join(note_parts) if note_parts else row["notes"] or ""
    c.execute("UPDATE jobs SET applied=?, notes=? WHERE job_id=?", (stamp, full_note, job_id))
    c.commit()
    audit("JOB_APPLIED", job_id,
          f"{row['company']} - {row['title']} | Applied: {stamp} | Link: {apply_url or row['url']}",
          f"ALSO LOG ON JOBSUIT: {JOBSUIT_URL} -> Add job manually with link: {apply_url or row['url']}")
    print(f"\n✓ Marked applied: {row['company']} - {row['title']}")
    print(f"  Timestamp : {stamp}")
    print(f"  Job link  : {apply_url or row['url']}")
    print(f"  Job ID    : {job_id}")
    print(f"\n  ⚠ OPEN JOBSUIT NOW and add this job:")
    print(f"  {JOBSUIT_URL}")
    print(f"  Paste link: {apply_url or row['url']}")
    C.build_excel()
    return True


def run_jobs(auto=False, notify=True):
    c = C.db()
    C.sync_contacts()
    s = requests.Session()
    s.headers.update(HEADERS)
    first_run = c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0

    # companies from the contact sheets + domains seen for each
    comp_roots = {}
    for r in c.execute("SELECT company, email FROM contacts WHERE status!='skipped'"):
        comp_roots.setdefault(r["company"], Counter())[C.domain_root(r["email"])] += 1
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    watch = load_watchlist()

    for r in c.execute("SELECT job_id, url FROM jobs WHERE url_status LIKE 'BROKEN%'").fetchall():
        c.execute("UPDATE jobs SET url_status=? WHERE job_id=?", (check_link(s, r["url"]), r["job_id"]))
    c.commit()

    boards, targets = [], set()
    print(f"Looking up job boards for {len(comp_roots)} companies (first run is slow, results are cached) ...")
    for company in sorted(comp_roots):
        w = watch.get(C._norm(company))
        if w:
            ent = w
        else:
            roots = [x for x, _ in comp_roots[company].most_common(2)]
            ent = discover(s, company, roots, cache)
        if ent.get("ats"):
            boards.append((company, ent["ats"], ent["slug"]))
    for key, w in watch.items():
        if not any(C._norm(b[0]) == key for b in boards):
            boards.append((w["company"], w["ats"], w["slug"]))
    CACHE.write_text(json.dumps(cache, indent=1))

    new_count = checked = 0
    for company, ats, slug in boards:
        try:
            listing = fetch_board(s, ats, slug)
        except (requests.RequestException, ValueError) as e:
            print(f"  {company}: could not read board ({type(e).__name__})")
            continue
        if listing is None:
            continue
        checked += 1
        source = f"{ats}:{slug}"
        live_ids = {f"{source}:{j['id']}" for j in listing}
        for r in c.execute("SELECT job_id FROM jobs WHERE source=? AND url_status NOT LIKE 'Closed%'", (source,)).fetchall():
            if r["job_id"] not in live_ids:
                c.execute("UPDATE jobs SET url_status='Closed (removed from company board)' WHERE job_id=?", (r["job_id"],))
        for j in listing:
            job_id = f"{source}:{j['id']}"
            if c.execute("SELECT 1 FROM jobs WHERE job_id=?", (job_id,)).fetchone():
                continue
            fit = classify_job(j["title"])
            if not fit or not in_india(j["location"]):
                continue
            status = check_link(s, j["url"])
            people = [r[0] for r in c.execute(
                "SELECT email FROM contacts WHERE company=? AND status NOT IN ('bounced','failed') LIMIT 5", (company,))]
            alumni = "https://www.linkedin.com/search/results/people/?keywords=" + quote(f"NIT Durgapur {company}")
            recr = "https://www.linkedin.com/search/results/people/?keywords=" + quote(f"{company} recruiter talent acquisition")
            c.execute("INSERT INTO jobs(job_id,company,title,location,url,url_status,fit,source,found_at,posted_at,"
                      "referrals,alumni_link,recruiter_link) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (job_id, company, j["title"], j["location"], j["url"], status, fit, source, C.iso(),
                       j["posted"], "; ".join(people), alumni, recr))
            c.commit()
            new_count += 1
            audit("JOB_FOUND", f"{company} - {j['title']}", f"{j['location']} | {fit} | {j['url']} | {status}",
                  f"Review and apply: {j['url']}" if status.startswith(("OK", "UNVERIFIED")) else
                  "Link not working yet - I will re-check it automatically")
    c.commit()

    pending = [dict(r) for r in c.execute(
        "SELECT * FROM jobs WHERE notified=0 AND (url_status LIKE 'OK%' OR url_status LIKE 'UNVERIFIED%') "
        "ORDER BY CASE WHEN fit LIKE 'Strong%' THEN 0 ELSE 1 END, company")]

    # Filter: keep jobs where salary is at or above MIN_SALARY_LPA, or salary is not mentioned at all
    def salary_ok(j):
        lpa = salary_lpa((j.get("title") or "") + " " + (j.get("notes") or ""))
        return lpa is None or lpa >= MIN_SALARY_LPA

    pending_filtered = [j for j in pending if salary_ok(j)]

    if pending_filtered and notify:
        strong = [j for j in pending_filtered if "Strong" in (j.get("fit") or "")]
        label = f"{len(pending_filtered)} new fresher opening(s)"
        if strong:
            label += f" incl. {len(strong)} Strong fit"
        if first_run:
            msg = (f"{len(pending_filtered)} fresher-friendly openings found (≥{MIN_SALARY_LPA} LPA or unspecified) "
                   f"across {checked} company boards. See Excel → Jobs (Fresher).")
            desktop_notify("Job watcher: first scan done", msg)
            email_self(f"Job watcher: {len(pending_filtered)} fresher openings found (≥{MIN_SALARY_LPA} LPA)",
                       msg + "\n\n" + "\n".join(describe(j) for j in pending_filtered[:40]),
                       jobs_list=pending_filtered[:40])
            audit("NOTIFIED", "", msg, "Open Job_Outreach_Tracker.xlsx -> 'Jobs (Fresher)' and apply to the Strong ones first")
        else:
            for j in pending_filtered[:3]:
                desktop_notify(f"New job: {j['company']}", f"{j['title']} - {j['fit']}")
            if len(pending_filtered) > 3:
                desktop_notify("Job watcher", f"+{len(pending_filtered) - 3} more new openings. Check the Excel.")
            top = pending_filtered[0]
            email_self(
                f"NEW fresher job(s): {top['title']} @ {top['company']}"
                + (f" (+{len(pending_filtered) - 1} more)" if len(pending_filtered) > 1 else "")
                + f" [≥{MIN_SALARY_LPA} LPA]",
                "\n".join(describe(j) for j in pending_filtered),
                jobs_list=pending_filtered,
            )
            audit("NOTIFIED", "", f"{len(pending_filtered)} new openings sent as notification",
                  "Apply soon: " + "; ".join(f"{j['company']} - {j['title']}" for j in pending_filtered[:5]))
    # Mark ALL as notified (including below-salary ones, so they don't spam on next run)
    for j in pending:
        c.execute("UPDATE jobs SET notified=1 WHERE job_id=?", (j["job_id"],))
    c.commit()
    if new_count or not auto:
        audit("JOBS_SUMMARY", "", f"Checked {checked} company boards, {new_count} new matching openings",
              "" if new_count == 0 else "See Excel -> Jobs (Fresher)")
    return 0
