"""
Run this ONCE after you put the correct Gmail App Password in .env
to send a live demo to sangamrevathi05@gmail.com.

Usage:
    python send_demo.py
"""
import sys
import common as C
import outreach

TO = "sangamrevathi05@gmail.com"

if not C.GMAIL_APP_PASSWORD or C.GMAIL_APP_PASSWORD == "Revathi@15.":
    print("ERROR: Put your Gmail App Password in the .env file first.")
    print("       It looks like:  abcd efgh ijkl mnop  (16 chars, spaces OK)")
    print("       Get one at: https://myaccount.google.com/apppasswords")
    sys.exit(1)

plain, h = outreach.render("Google", "")
msg, mid = outreach.build_msg(TO, outreach.SUBJECT, plain, h, attach=True)

print("Sending demo email...")
print(f"  TO     : {TO}")
print(f"  SUBJECT: {outreach.SUBJECT}")
print(f"  ATTACH : {C.RESUME.name} ({round(C.RESUME.stat().st_size/1024)} KB)")
print()

mailer = outreach.Mailer()
try:
    mailer.send(msg)
    print("SUCCESS - check sangamrevathi05@gmail.com inbox.")
    C.audit("DEMO_SENT", TO, f"Demo email sent to {TO}", "Check inbox to verify subject, body, and resume attachment")
finally:
    mailer.close()
    try:
        C.build_excel()
        print(f"Excel updated: {C.EXCEL_OUT}")
    except Exception as e:
        print(f"(Excel update skipped: {e})")
