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
import re
import socket
import sys

# A stalled IMAP connection must fail, not hang - a hung fetch once blocked the
# whole refresh pipeline for hours.
socket.setdefaulttimeout(60)

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

# Gmail-only fast path: have the server find spreadsheet attachments instead
# of downloading 200 full messages to look inside each one (~5 min per run).
# Zero matches is a real answer (reports not emailed yet) - only fall back to
# the full scan if the extension itself fails.
try:
    _, ids = M.search(None, "X-GM-RAW", '"has:attachment (filename:xlsx OR filename:xls)"')
except imaplib.IMAP4.error:
    _, ids = M.search(None, "ALL")
msg_ids = ids[0].split()[-LOOKBACK:][::-1]  # newest first

found = {}


def _save_from_msg(mid):
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


def _fetch_structures(ids):
    """{msg_id: lowercased BODYSTRUCTURE bytes} - attachment names without
    downloading any message body, all in one round trip."""
    if not ids:
        return {}
    _, raw = M.fetch(b",".join(ids), "(BODYSTRUCTURE)")
    out = {}
    for item in raw:
        if isinstance(item, tuple):
            item = b" ".join(p for p in item if isinstance(p, bytes))
        if not isinstance(item, bytes):
            continue
        m = re.match(rb"(\d+) ", item)
        if m:
            out[m.group(1)] = item.lower()
    return out


# Download full messages only where the structure shows a matching filename -
# reading every candidate in full used to cost ~5 minutes per run.
structures = _fetch_structures(msg_ids)
for mid in msg_ids:
    if len(found) == len(REPORTS):
        break
    st = structures.get(mid, b"")
    if any(key.encode() in st for key in REPORTS if key not in found):
        _save_from_msg(mid)

# Rare encodings can hide the filename from BODYSTRUCTURE - as a last resort
# read the newest few messages in full like the old scanner did.
if len(found) < len(REPORTS):
    for mid in msg_ids[:25]:
        if len(found) == len(REPORTS):
            break
        _save_from_msg(mid)

M.logout()

missing = [k for k in REPORTS if k not in found]
if missing:
    print(f"WARNING: no attachment found for: {missing} in newest {LOOKBACK} messages.")
    # still exit 0 - parser will use whatever data exists
print(f"Done: {len(found)}/{len(REPORTS)} reports fetched.")
