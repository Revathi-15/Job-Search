"""job_boards.py - search LinkedIn, Indeed, Glassdoor and Naukri for fresher SDE jobs.

Uses python-jobspy (pip install -U python-jobspy) which calls the public
guest endpoints of each board — no login, no API key needed.

Run:
    python job_boards.py              # search and email results to yourself
    python job_boards.py --hours 48   # widen to 48 hours
    python job_boards.py --dry-run    # print results, don't email or save to DB
"""
import argparse
import html as _html
import re
import smtplib
import ssl
import sys
import time
import traceback
from email.message import EmailMessage
from urllib.parse import quote

import common as C
from common import audit

# ------------------------------------------------------------------ search config
SITES      = ["linkedin", "indeed", "glassdoor", "naukri"]
LOCATIONS  = ["India", "Bengaluru", "Hyderabad", "Pune", "Chennai", "Mumbai", "Gurugram", "Noida"]
HOURS_OLD  = int(C.env("JOB_HOURS_OLD", "24"))       # only jobs posted in last N hours
RESULTS_PER_SITE = int(C.env("JOB_RESULTS_PER_SITE", "50"))  # per site, per search term

# Search terms tuned for a 2026 B.Tech CSE graduate, India job market
# LinkedIn / Indeed / Glassdoor use these; Naukri uses a separate set below
SEARCH_TERMS = [
    "software engineer 2026 batch india",
    "SDE 1 2026 passout india",
    "software engineer entry level 2026",
    "backend engineer 2026 graduate india",
    "associate software engineer 2026",
    "junior software engineer 2026",
]

# Naukri uses Indian terminology — "2026 batch", "2026 passout", "fresher"
NAUKRI_TERMS = [
    "software engineer 2026 batch",
    "SDE 2026 passout",
    "software developer 2026 fresher",
    "backend developer 2026 batch",
    "associate software engineer 2026",
]

# Delay between search terms (seconds) — avoids LinkedIn 429
INTER_TERM_DELAY = int(C.env("JOB_INTER_TERM_DELAY", "8"))

# ------------------------------------------------------------------ filters (mirror outreach classify_job)
TECH_RX    = re.compile(
    r"\b(software|sde|swe|developer|engineer(ing)?|backend|back.?end|full.?stack|"
    r"front.?end|devops|sre|machine.?learning|ml|ai|member.of.technical.staff|mts)\b", re.I)
NON_TECH_RX = re.compile(
    r"\b(sales|solutions?\s+engineer|support|customer|mechanical|electrical|civil|"
    r"manufacturing|hardware|field\s+engineer|recruiter|marketing|finance|legal|"
    r"account\s+(executive|manager)|technician)\b", re.I)
SENIOR_RX  = re.compile(
    r"\b(senior|sr\.?|staff|principal|lead|manager|director|architect|head|vp|"
    r"distinguished|fellow|ii|iii|iv|l[3-9]|e[4-9])\b", re.I)
INTERN_RX  = re.compile(r"\b(intern|internship)\b", re.I)
STRONG_RX  = re.compile(
    r"\b(sde.?(1|i\b)|software\s+(development\s+)?engineer.?(1|i\b)|"
    r"new.?grad(uate)?|graduate|entry.?level|fresher|junior|associate|trainee|"
    r"early.?career|campus|university|apprentice|"
    r"2026\s*(batch|passout|pass.?out|graduate)?|2025\s*(batch|passout)?|2027\s*(batch|passout)?)\b", re.I)

INDIA_KW = {"india", "bengaluru", "bangalore", "hyderabad", "pune", "chennai",
            "mumbai", "gurgaon", "gurugram", "noida", "delhi", "kolkata",
            "ahmedabad", "kochi", "coimbatore", "jaipur", "indore", "remote"}


def classify(title, location=""):
    if not TECH_RX.search(title):
        return None
    if NON_TECH_RX.search(title):
        return None
    if SENIOR_RX.search(title):
        return None
    loc = (location or "").lower()
    if loc and not any(k in loc for k in INDIA_KW):
        return None   # not India / remote
    if INTERN_RX.search(title) and not re.search(r"graduate|new grad|full.?time", title, re.I):
        return "Internship"
    if STRONG_RX.search(title):
        return "Strong (entry-level)"
    return "Possible (check experience needed)"


def salary_ok(row):
    """Return True if max salary ≥ MIN_SALARY_LPA, or no salary data at all."""
    min_lpa = float(C.env("MIN_SALARY_LPA", "18"))
    mx = getattr(row, "max_amount", None)
    if mx is None or (isinstance(mx, float) and mx != mx):  # NaN
        return True
    interval = str(getattr(row, "interval", "") or "").lower()
    currency  = str(getattr(row, "currency", "") or "").upper()
    # Convert to annual INR LPA
    try:
        mx = float(mx)
    except (TypeError, ValueError):
        return True
    if currency in ("INR", ""):
        if interval == "yearly":
            lpa = mx / 100_000
        elif interval == "monthly":
            lpa = mx * 12 / 100_000
        else:
            return True   # can't determine
    elif currency == "USD":
        # rough: 1 USD ≈ 84 INR (2026)
        lpa = mx * 84 / 100_000 if interval == "yearly" else mx * 84 * 2080 / 100_000
    else:
        return True  # unknown currency → don't filter out
    return lpa >= min_lpa


# ------------------------------------------------------------------ dedup against existing DB jobs
def already_seen(c, job_url):
    return bool(c.execute("SELECT 1 FROM board_jobs WHERE job_url=?", (job_url,)).fetchone())


BOARD_SCHEMA = """
CREATE TABLE IF NOT EXISTS board_jobs(
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  site        TEXT,
  job_url     TEXT UNIQUE,
  title       TEXT,
  company     TEXT,
  location    TEXT,
  date_posted TEXT,
  job_type    TEXT,
  min_salary  REAL,
  max_salary  REAL,
  currency    TEXT,
  interval    TEXT,
  fit         TEXT,
  description TEXT,
  found_at    TEXT,
  notified    INTEGER DEFAULT 0
);
"""


def ensure_schema(c):
    c.executescript(BOARD_SCHEMA)
    c.commit()


# ------------------------------------------------------------------ email
def _row_html(j):
    fit_color = "#2e7d32" if "Strong" in j["fit"] else "#e65100"
    sal = ""
    if j.get("max_salary") and j["max_salary"] == j["max_salary"]:  # not NaN
        unit = "LPA" if str(j.get("currency", "")).upper() in ("INR", "") else j.get("currency", "")
        val  = round(j["max_salary"] / 100_000, 1) if str(j.get("currency", "")).upper() in ("INR", "") \
               else round(j["max_salary"], 0)
        sal  = f"<br><small>Up to {val} {unit}/yr</small>"
    li_search = "https://www.linkedin.com/search/results/people/?keywords=" + quote(
        f"NIT Durgapur {j['company']}")
    rec_search = "https://www.linkedin.com/search/results/people/?keywords=" + quote(
        f"{j['company']} recruiter talent acquisition")
    return f"""
<tr>
  <td style='padding:10px;border-bottom:1px solid #eee;vertical-align:top;'>
    <strong>{_html.escape(j['company'])}</strong><br>
    <small style='color:#666;'>{_html.escape(j['site'].title())}</small>
    {sal}
  </td>
  <td style='padding:10px;border-bottom:1px solid #eee;vertical-align:top;'>
    <strong>{_html.escape(j['title'])}</strong><br>
    <span style='color:{fit_color};font-size:12px;'>{j['fit']}</span>
  </td>
  <td style='padding:10px;border-bottom:1px solid #eee;vertical-align:top;'>{_html.escape(j['location'] or '')}</td>
  <td style='padding:10px;border-bottom:1px solid #eee;vertical-align:top;'>
    <a href='{j['job_url']}' style='background:#1565c0;color:#fff;padding:6px 14px;
       border-radius:4px;text-decoration:none;font-weight:bold;font-size:13px;'>APPLY NOW</a><br>
    <small style='color:#888;'>{j['date_posted'] or ''}</small>
  </td>
  <td style='padding:10px;border-bottom:1px solid #eee;vertical-align:top;font-size:12px;'>
    <a href='{li_search}'>NIT DGP alumni</a><br>
    <a href='{rec_search}'>Recruiters</a>
  </td>
</tr>"""


def send_email(jobs_list, hours_old):
    if not (C.NOTIFY_EMAIL and C.GMAIL_APP_PASSWORD):
        print("(email skipped: NOTIFY_EMAIL=false or no GMAIL_APP_PASSWORD)")
        return False

    strong  = [j for j in jobs_list if "Strong" in j["fit"]]
    total   = len(jobs_list)
    subject = (f"🎯 {total} fresher SDE job(s) posted in last {hours_old}h"
               + (f" — {len(strong)} Strong fit" if strong else "")
               + " | LinkedIn/Indeed/Glassdoor/Naukri")

    plain = f"{total} fresher SDE jobs posted in last {hours_old} hours.\n\n"
    for j in jobs_list:
        plain += (f"[{j['fit']}] {j['title']} @ {j['company']} ({j['location']})\n"
                  f"  Apply: {j['job_url']}\n"
                  f"  Posted: {j['date_posted']}  Source: {j['site']}\n\n")

    rows_html = "".join(_row_html(j) for j in jobs_list)
    min_lpa   = C.env("MIN_SALARY_LPA", "18")
    html_body = f"""
<html><body style='font-family:Arial,sans-serif;max-width:960px;margin:auto;padding:16px;'>
<h2 style='color:#1565c0;margin-bottom:4px;'>
  🎯 {total} Fresher SDE Job(s) — Last {hours_old} Hours
</h2>
<p style='color:#555;margin-top:4px;'>
  Sources: LinkedIn · Indeed · Glassdoor · Naukri &nbsp;|&nbsp;
  Salary filter: ≥{min_lpa} LPA (or not listed) &nbsp;|&nbsp;
  <strong style='color:#2e7d32;'>Green = Strong fit &nbsp; Orange = Possible</strong>
</p>
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
<hr style='margin-top:24px;'>
<p style='font-size:11px;color:#aaa;'>
  Auto-sent by your job-search script · Run again: <code>python job_boards.py</code>
</p>
</body></html>"""

    m = EmailMessage()
    m["From"]    = f"Job Watcher <{C.GMAIL_USER}>"
    m["To"]      = C.GMAIL_USER
    m["Subject"] = subject
    m.set_content(plain)
    m.add_alternative(html_body, subtype="html")
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465,
                               context=ssl.create_default_context(), timeout=60) as s:
            s.login(C.GMAIL_USER, C.GMAIL_APP_PASSWORD)
            s.send_message(m)
        print(f"Email sent to {C.GMAIL_USER}")
        return True
    except Exception as e:
        print(f"Email failed: {e}")
        return False


# ------------------------------------------------------------------ main
def run(hours_old=HOURS_OLD, dry_run=False):
    try:
        from jobspy import scrape_jobs
    except ImportError:
        print("ERROR: python-jobspy not installed. Run:  pip install -U python-jobspy")
        return 1

    c = C.db()
    ensure_schema(c)
    new_jobs = []
    seen_urls = set()

    # --- Phase 1: LinkedIn + Indeed + Glassdoor (together, rate-limit friendly)
    li_sites = ["linkedin", "indeed", "glassdoor"]
    print(f"Searching {', '.join(li_sites)} for fresher SDE jobs posted in last {hours_old}h ...")
    print(f"Search terms: {len(SEARCH_TERMS)}")
    print()

    for i, term in enumerate(SEARCH_TERMS):
        print(f"  [{i+1}/{len(SEARCH_TERMS)}] '{term}' ...")
        try:
            df = scrape_jobs(
                site_name          = li_sites,
                search_term        = term,
                location           = "India",
                results_wanted     = RESULTS_PER_SITE,
                hours_old          = hours_old,
                country_indeed     = "India",
                verbose            = 0,
                description_format = "plain",
            )
        except Exception as e:
            print(f"    scrape failed: {e}")
            df = None

        matched = _process_df(df, seen_urls, new_jobs, c, dry_run)
        print(f"    {len(df) if df is not None else 0} raw → {matched} new matches")

        if i < len(SEARCH_TERMS) - 1:
            time.sleep(INTER_TERM_DELAY)

    # --- Phase 2: Naukri separately (different search terms, no location needed)
    print()
    print(f"Searching Naukri (India) ...")
    for i, term in enumerate(NAUKRI_TERMS):
        print(f"  [{i+1}/{len(NAUKRI_TERMS)}] '{term}' ...")
        try:
            df = scrape_jobs(
                site_name          = ["naukri"],
                search_term        = term,
                location           = "India",
                results_wanted     = RESULTS_PER_SITE,
                hours_old          = hours_old,
                country_indeed     = "India",
                verbose            = 0,
                description_format = "plain",
            )
        except Exception as e:
            print(f"    naukri scrape failed: {e}")
            df = None

        matched = _process_df(df, seen_urls, new_jobs, c, dry_run)
        print(f"    {len(df) if df is not None else 0} raw → {matched} new matches")

        if i < len(NAUKRI_TERMS) - 1:
            time.sleep(4)

    # Deduplicate by URL across all search terms
    seen = set()
    deduped = []
    for j in new_jobs:
        if j["job_url"] not in seen:
            seen.add(j["job_url"])
            deduped.append(j)

    # Sort: Strong first, then by company name
    deduped.sort(key=lambda j: (0 if "Strong" in j["fit"] else 1, j["company"].lower()))

    print()
    print(f"Total new matches (deduped): {len(deduped)}")
    if deduped:
        print()
        print(f"  {'FIT':<28} {'COMPANY':<25} {'TITLE':<45} {'SOURCE'}")
        print("  " + "-" * 108)
        for j in deduped:
            print(f"  {j['fit']:<28} {j['company'][:24]:<25} {j['title'][:44]:<45} {j['site']}")

    if dry_run:
        print("\n(dry-run: nothing saved or emailed)")
        return 0

    # Save to DB
    saved = 0
    for j in deduped:
        try:
            c.execute(
                "INSERT OR IGNORE INTO board_jobs"
                "(site,job_url,title,company,location,date_posted,job_type,"
                " min_salary,max_salary,currency,interval,fit,description,found_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (j["site"], j["job_url"], j["title"], j["company"], j["location"],
                 j["date_posted"], j["job_type"], j.get("min_salary"), j.get("max_salary"),
                 j["currency"], j["interval"], j["fit"], j["description"], j["found_at"])
            )
            if c.execute("SELECT changes()").fetchone()[0]:
                saved += 1
        except Exception as e:
            print(f"  DB insert failed for {j['job_url']}: {e}")
    c.commit()
    print(f"Saved {saved} new jobs to DB.")

    if deduped:
        audit("BOARD_JOBS_FOUND", "",
              f"{len(deduped)} fresher SDE jobs from LinkedIn/Indeed/Glassdoor/Naukri (last {hours_old}h)",
              f"Check email {C.GMAIL_USER} for apply links")
        send_email(deduped, hours_old)
    else:
        print("No new jobs matching your profile found in this window.")
        audit("BOARD_JOBS_SCAN", "",
              f"Searched {len(SEARCH_TERMS)+len(NAUKRI_TERMS)} terms across all boards, 0 new matches")

    return 0


def _process_df(df, seen_urls, new_jobs, c, dry_run):
    """Extract matching jobs from a DataFrame, return count of new matches."""
    if df is None or len(df) == 0:
        return 0
    matched = 0
    for _, row in df.iterrows():
        url = str(row.get("job_url") or "")
        if not url or url in seen_urls:
            continue
        title = str(row.get("title") or "")
        loc   = str(row.get("location") or "")
        fit   = classify(title, loc)
        if not fit:
            continue
        if not salary_ok(row):
            continue
        if not dry_run and already_seen(c, url):
            seen_urls.add(url)
            continue
        seen_urls.add(url)

        def v(col, default=""):
            val = row.get(col)
            if val is None or (isinstance(val, float) and val != val):
                return default
            return str(val).strip() or default

        new_jobs.append(dict(
            site        = v("site"),
            job_url     = url,
            title       = title,
            company     = v("company"),
            location    = loc,
            date_posted = v("date_posted"),
            job_type    = v("job_type"),
            min_salary  = row.get("min_amount"),
            max_salary  = row.get("max_amount"),
            currency    = v("currency"),
            interval    = v("interval"),
            fit         = fit,
            description = v("description")[:500],
            found_at    = C.iso(),
        ))
        matched += 1
    return matched


def main():
    p = argparse.ArgumentParser(description="Search LinkedIn/Indeed/Glassdoor/Naukri for fresher SDE jobs")
    p.add_argument("--hours",    type=int, default=HOURS_OLD,
                   help=f"only jobs posted within this many hours (default {HOURS_OLD})")
    p.add_argument("--dry-run",  action="store_true",
                   help="print results only, don't save to DB or send email")
    p.add_argument("--results",  type=int, default=RESULTS_PER_SITE,
                   help=f"results per site per search term (default {RESULTS_PER_SITE})")
    a = p.parse_args()
    try:
        sys.exit(run(hours_old=a.hours, dry_run=a.dry_run))
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(130)
    except Exception as e:
        print(f"CRASH: {e}")
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
