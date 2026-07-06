#!/usr/bin/env python3
"""
Pull the newest ServiceTitan report attachments from Gmail via IMAP.

Env vars (set as GitHub Actions secrets):
    GMAIL_ADDRESS       rjmageelvclaude@gmail.com
    GMAIL_APP_PASSWORD  16-char Google app password (requires 2-Step Verification)

Finds the most recent email whose attachment filename matches each report pattern
and saves them to data/sales_by_rep.xlsx and data/installed_by_rep.xlsx.
Exits 0 with a notice (and downloads nothing) if credentials are missing, so the
workflow can fall back to the last committed data.
"""
import email
import imaplib
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.path.dirname(HERE), "data")

REPORTS = {
    # keyword (lowercased) that must appear in the attachment filename -> output file
    "sales by rep": "sales_by_rep.xlsx",
    "installed rev by rep": "installed_by_rep.xlsx",
}
LOOKBACK = 200  # newest N messages to scan

addr = os.environ.get("GMAIL_ADDRESS", "").strip()
pw = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()
if not addr or not pw:
    print("NOTICE: GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set - skipping fetch, "
          "will use last committed data.")
    sys.exit(0)

os.makedirs(DATA, exist_ok=True)

M = imaplib.IMAP4_SSL("imap.gmail.com")
M.login(addr, pw)
M.select("INBOX", readonly=True)

_, ids = M.search(None, "ALL")
msg_ids = ids[0].split()[-LOOKBACK:][::-1]  # newest first

found = {}
for mid in msg_ids:
    if len(found) == len(REPORTS):
        break
    _, raw = M.fetch(mid, "(RFC822)")
    msg = email.message_from_bytes(raw[0][1])
    for part in msg.walk():
        fn = part.get_filename()
        if not fn or not fn.lower().endswith((".xlsx", ".xls")):
            continue
        low = fn.lower()
        for key, out in REPORTS.items():
            if key in low and key not in found:
                path = os.path.join(DATA, out)
                with open(path, "wb") as f:
                    f.write(part.get_payload(decode=True))
                found[key] = fn
                print(f"Saved {out}  <-  '{fn}'  (msg {mid.decode()}, {msg.get('Date','')})")

M.logout()

missing = [k for k in REPORTS if k not in found]
if missing:
    print(f"WARNING: no attachment found for: {missing} in newest {LOOKBACK} messages.")
    # still exit 0 - parser will use whatever data exists
print(f"Done: {len(found)}/{len(REPORTS)} reports fetched.")
