import common as C

c = C.db()

# Find and permanently skip your own email addresses
own_emails = [
    "sangamrevathi05@gmail.com",
    "revathisangam2005@gmail.com",
]

for em in own_emails:
    r = c.execute("SELECT email, status, source FROM contacts WHERE email=?", (em,)).fetchone()
    if r:
        print(f"Found: {r['email']}  status={r['status']}  source={r['source']}")
        c.execute(
            "UPDATE contacts SET status='skipped', note='Own email - never send to self' WHERE email=?",
            (em,)
        )
        print(f"  -> Marked as skipped")
    else:
        print(f"Not in DB: {em}")

c.commit()
print("\nDone. These will never be mailed.")
