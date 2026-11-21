"""run.py - one command to do everything.

    python run.py          # send pending outreach emails + search for new jobs
    python run.py --jobs-only   # skip email sending, just search jobs
    python run.py --mail-only   # skip job search, just send emails
    python run.py --hours 48    # search jobs posted in last 48h (default 24)
"""
import argparse
import sys
import traceback

import common as C
from common import audit


def main():
    p = argparse.ArgumentParser(description="Send outreach emails + search for fresher SDE jobs")
    p.add_argument("--jobs-only", action="store_true", help="skip email sending, just search jobs")
    p.add_argument("--mail-only", action="store_true", help="skip job search, just send emails")
    p.add_argument("--hours",     type=int, default=24, help="job search window in hours (default 24)")
    p.add_argument("--dry-run",   action="store_true", help="preview jobs only, don't email or save")
    a = p.parse_args()

    exit_code = 0

    # ------------------------------------------------------------------ Step 1: Send outreach emails
    if not a.jobs_only:
        print("=" * 60)
        print("STEP 1: Sending outreach emails")
        print("=" * 60)
        try:
            import send_batch   # send_batch runs at import time (it's a script)
        except SystemExit as e:
            # send_batch calls sys.exit() — that's fine, capture it
            if e.code not in (0, None):
                exit_code = e.code
        except Exception as e:
            print(f"Email send failed: {e}")
            traceback.print_exc()
            exit_code = 1
        print()

    # ------------------------------------------------------------------ Step 2: Search job boards
    if not a.mail_only:
        print("=" * 60)
        print(f"STEP 2: Searching job boards (last {a.hours}h)")
        print("=" * 60)
        try:
            import job_boards
            code = job_boards.run(hours_old=a.hours, dry_run=a.dry_run)
            if code:
                exit_code = code
        except Exception as e:
            print(f"Job search failed: {e}")
            traceback.print_exc()
            exit_code = 1

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
