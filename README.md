# Job Outreach Automation (Revathi Sangam)

One folder, one command. It mails everyone in your Excel sheets (once), tracks replies, builds an Excel
tracker + audit log, and watches for fresher job openings.

## Folder must contain
`outreach.py  common.py  jobs.py  requirements.txt  .env  REVATHI_SANGAM.pdf`
and your two contact sheets (`Employee_Emails_List_MNC_s.xlsx`, `Companies_and_Employees_Emails.xlsx`).
Any other `.xlsx` you drop in this folder is also scanned for emails.

## One-time setup
1. Install Python 3.9+ (python.org). Open a terminal in this folder.
2. `pip install -r requirements.txt`
3. Gmail App Password: turn on 2-Step Verification -> Google Account -> Security -> App passwords -> create one.
   Copy `.env.example` to `.env` and paste the 16 characters after `GMAIL_APP_PASSWORD=`.
   (Gmail Settings -> Forwarding and POP/IMAP -> make sure IMAP is enabled; needed to read replies.)
4. `python outreach.py send --dry-run`   -> shows counts + sample mails, sends NOTHING
5. `python outreach.py send --test`      -> ONE mail to yourself; check it in Gmail
6. `python outreach.py run`              -> real sending

## Commands
| Command | What it does |
|---|---|
| `python outreach.py run` | checks replies, then sends pending mails (only Mon-Fri 10:30-12:30 and 14:00-16:00 IST) |
| `python outreach.py run --wait` | if it is outside the window, waits for the next one |
| `python outreach.py send --dry-run` | preview only |
| `python outreach.py followup --dry-run` | who would get a follow-up |
| `python outreach.py followup --min-days 5` | follow-up (once per person) to those mailed 5+ days ago with no reply |
| `python outreach.py replies` | read Gmail inbox for replies and bounces |
| `python outreach.py jobs` | look for fresher openings, notify on new ones |
| `python outreach.py status` / `report` | quick counts / rebuild the Excel |

## Rules built in
- Each email address is mailed **once**. State lives in `tracker.db`; a mail is recorded before it is sent, and
  an interrupted one is marked `uncertain` and never auto-retried.
- Max 60 mails/day (`DAILY_LIMIT`), max 5 per company per day, 60-120 s between mails, companies interleaved.
- Personal addresses (gmail/yahoo/icloud ...) and no-reply addresses are skipped.
- Follow-up only when YOU run it; it first re-checks replies and never follows up with people who replied/bounced.

## Files it creates
- `Job_Outreach_Tracker.xlsx` - sheets: Summary, Contacts & Mails, Jobs (Fresher), Audit Log.
  Columns "Applied?", "Notes", "My Notes" are yours - they survive regeneration. Close the file in Excel before a run
  if possible (otherwise a timestamped copy is saved).
- `audit_log.csv` - every action with an "Action needed from you" column. Also the Audit Log sheet.
- `tracker.db`, `ats_cache.json`, `*.lock` - internal. Don't delete tracker.db or you lose the sent history
  (and could re-mail people).

## Automatic runs (optional)
The scripts exit quietly outside sending hours, so you can schedule them often.

Windows (Command Prompt; change the path):
```
schtasks /Create /SC MINUTE /MO 15 /TN "OutreachRun"  /TR "cmd /c cd /d C:\path\job_outreach && python outreach.py run --auto"
schtasks /Create /SC MINUTE /MO 30 /TN "OutreachJobs" /TR "cmd /c cd /d C:\path\job_outreach && python outreach.py jobs --auto"
```
Mac/Linux (`crontab -e`):
```
*/15 * * * 1-5 cd /path/job_outreach && python3 outreach.py run --auto  >> cron.log 2>&1
*/30 * * * *   cd /path/job_outreach && python3 outreach.py jobs --auto >> cron.log 2>&1
```
The computer must be on (not asleep) at those times. `NOTIFY_EMAIL=true` also emails new jobs to your own Gmail,
so you get a phone notification from the Gmail app.

## Honest limitations
- Job watching uses the public APIs of Greenhouse, Lever and Ashby only. Companies with their own career site
  (Amazon, Walmart, Oracle, banks ...) are NOT covered. Add known boards in `watchlist.csv`.
- Slugs are auto-guessed from company names; a wrong guess is possible - always check the "Apply link".
- No LinkedIn scraping (against their terms). The "alumni" and "recruiter" columns are ready-made LinkedIn
  searches (e.g. "NIT Durgapur <Company>") that you open and browse yourself; "People at this company from YOUR
  email list" are real addresses from your sheets.
- Reply type (positive / negative / auto-reply) is a keyword guess. Always read the actual reply.
- Gmail may rate-limit or flag bulk cold mail. Keep the daily limit modest.
