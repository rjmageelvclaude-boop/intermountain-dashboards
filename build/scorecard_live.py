#!/usr/bin/env python3
"""
Live ServiceTitan engine for the printable employee Scorecard.

One page per employee: KPIs for the current payroll period ("week"), MTD and
YTD, team ranks, and gross-pay pacing toward an annual target - printed and
signed by the employee and their manager.

Foundation release covers the HVAC service tech role; the ROLES registry is
built for the other boards (CSR, installer, CA, plumber, silo) to slot in the
same way:

  MTD / YTD KPIs   read from the sibling board's data.json (already computed,
                   cached and verified by that board's engine) - the scorecard
                   runs AFTER the sibling boards in refresh.yml, like the GOAT
                   board.
  Week KPIs        computed here with the sibling engine's own window function
                   (tech_board_live.compute_window) over the current payroll
                   period, detected per company from the payrolls feed
                   (SIE weekly Fri-start, ULT biweekly Sun, RUS weekly).
  Pay              per-employee gross = sum of payroll gross-pay-item amounts
                   (TimesheetTime wages + InvoiceRelatedBonus performance pay
                   + every other type), with a per-type breakdown kept for
                   validation. Closed months cached in
                   data/scorecard-pay-history.json.
  Pay privacy      the pay block never ships in plaintext: it is encrypted
                   with AES-256-GCM under a passphrase (env SCORECARD_KEY /
                   GitHub secret) and unlocked in the browser by the manager.
                   No SCORECARD_KEY -> data.json simply has no pay section.
  Goals            KPI goal table read from config/scorecard-goals.json
                   (committed defaults), overridden by the shared Apps Script
                   store (action getScorecardGoals) once build/scorecard-goals.gs
                   is deployed - that's the manager-editable path. Annual pay
                   targets live inside the encrypted pay block, sourced from
                   the store (PIN-gated getScorecardPayTargets) or
                   secrets/scorecard-pay-targets.json locally.

CLI smoke test:
    py build/scorecard_live.py                # full payload summary
    py build/scorecard_live.py pay sierra     # this month's pay per roster tech
"""
import base64
import datetime as dt
import json
import os
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from command_center_live import (fetch_all, local_today, _load_json,
                                 _save_json, _utc_offset_hours, update_history)
import tech_board_live as tech_engine
from tech_board_live import COMPANIES

PAY_HISTORY_FILE = os.path.join(ROOT, "data", "scorecard-pay-history.json")
GOALS_DEFAULTS_FILE = os.path.join(ROOT, "config", "scorecard-goals.json")
PAY_TARGETS_LOCAL = os.path.join(ROOT, "secrets", "scorecard-pay-targets.json")
STORE_URL = ("https://script.google.com/macros/s/AKfycby-J4xARcDQoDUwSfX6qsjYnM"
             "_QZyDfhd09WVBQXomJkLsrDysnkw-L0EZu0_S9Q4kz-w/exec")
PAY_FREEZE_DAYS = 15         # payroll adjusts later than jobs - freeze later too
PAY_RECHECK_HOURS = 24
KDF_ITERATIONS = 310_000
AAD = b"hyperion-scorecard-v1"
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# ------------------------------------------------------------------ roles
# Each role: where MTD/YTD rows come from, how the week window is computed,
# and the metric schema the page renders (top to bottom).
ROLES = {
    "tech": {
        "label": "HVAC Service Technician",
        "board": "tech-board",          # sibling data.json with mtd/ytd rows
        "rankBy": "revenue",
        "metrics": [
            {"key": "revenue",  "label": "Revenue",          "fmt": "money"},
            {"key": "sales",    "label": "Total Sales",      "fmt": "money"},
            {"key": "jobs",     "label": "Completed Jobs",   "fmt": "int"},
            {"key": "avgTicket","label": "Average Ticket",   "fmt": "money0"},
            {"key": "convRate", "label": "Conversion Rate",  "fmt": "pct"},
            {"key": "membSold", "label": "Memberships Sold", "fmt": "int"},
            {"key": "tglSales", "label": "TGL Sales",        "fmt": "money"},
        ],
        "roster": tech_engine.team_technicians,
        "week": lambda company, day_from, day_to, roster: {
            tid: tech_engine._finalize(c)
            for tid, c in tech_engine.compute_window(
                company, day_from, day_to, roster=roster).items()},
    },
}


# ------------------------------------------------------------------ pay period
def current_pay_period(company):
    """(start_day, end_day) of the tenant-local payroll period containing
    today, from the payrolls feed. Monday-Sunday fallback."""
    co = COMPANIES[company]
    today = local_today(co["tz"])
    now = dt.datetime.utcnow()
    lookback = tech_engine._iso_z(
        dt.datetime(today.year, today.month, today.day) - dt.timedelta(days=16))
    best = None
    try:
        for p in fetch_all(co["tenant"], "/payroll/v2/tenant/{tenant}/payrolls",
                           {"startedOnOrAfter": lookback}, page_size=200, max_pages=20):
            try:
                s = dt.datetime.strptime(p["startedOn"][:19], "%Y-%m-%dT%H:%M:%S")
                e = dt.datetime.strptime(p["endedOn"][:19], "%Y-%m-%dT%H:%M:%S")
            except (KeyError, TypeError, ValueError):
                continue
            if s <= now < e and (best is None or s > best[0]):
                best = (s, e)
    except Exception:
        pass
    if best is not None:
        s, e = best
        start = (s + dt.timedelta(hours=_utc_offset_hours(co["tz"], s.date()))).date()
        # endedOn is the period's last instant (local end-of-day or next
        # midnight) - back off a second so .date() lands on the last day
        end = (e + dt.timedelta(hours=_utc_offset_hours(co["tz"], e.date()))
               - dt.timedelta(seconds=1)).date()
        return min(start, today), max(end, today)
    monday = today - dt.timedelta(days=today.weekday())
    return monday, monday + dt.timedelta(days=6)

def day_label(day):
    return f"{MONTHS[day.month - 1]} {day.day}"


# ------------------------------------------------------------------ pay
def _new_pay():
    return {"gross": 0.0, "adj": 0.0, "byType": {}, "regHours": 0.0, "otHours": 0.0}

def pay_window(tenant, day_from, day_to):
    """{employeeId: pay counters} for tenant-local days inclusive. Item `date`
    is the shift day (date-only), so the window is passed as plain dates.
    gross = sum of `amount` over every gross-pay-item type; `amountAdjustment`
    is tracked separately until validated against a real paycheck."""
    out = {}
    for i in fetch_all(tenant, "/payroll/v2/tenant/{tenant}/gross-pay-items",
                       {"dateOnOrAfter": day_from.isoformat(),
                        "dateOnOrBefore": day_to.isoformat()},
                       page_size=500, max_pages=400):
        eid = i.get("employeeId")
        if eid is None:
            continue
        rec = out.setdefault(eid, _new_pay())
        amt = float(i.get("amount") or 0)
        typ = i.get("grossPayItemType") or "Other"
        rec["gross"] += amt
        rec["adj"] += float(i.get("amountAdjustment") or 0)
        rec["byType"][typ] = rec["byType"].get(typ, 0.0) + amt
        if typ == "TimesheetTime":
            hrs = float(i.get("paidDurationHours") or 0)
            if i.get("paidTimeType") == "Overtime":
                rec["otHours"] += hrs
            else:
                rec["regHours"] += hrs
    for rec in out.values():
        rec["gross"] = round(rec["gross"], 2)
        rec["adj"] = round(rec["adj"], 2)
        rec["regHours"] = round(rec["regHours"], 2)
        rec["otHours"] = round(rec["otHours"], 2)
        rec["byType"] = {t: round(v, 2) for t, v in rec["byType"].items()}
    return out

def _sum_pay(dicts):
    total = _new_pay()
    for d in dicts:
        total["gross"] += d["gross"]
        total["adj"] += d["adj"]
        total["regHours"] += d["regHours"]
        total["otHours"] += d["otHours"]
        for t, v in d["byType"].items():
            total["byType"][t] = total["byType"].get(t, 0.0) + v
    total["gross"] = round(total["gross"], 2)
    total["adj"] = round(total["adj"], 2)
    total["regHours"] = round(total["regHours"], 2)
    total["otHours"] = round(total["otHours"], 2)
    total["byType"] = {t: round(v, 2) for t, v in total["byType"].items()}
    return total

def pay_months(company, deadline=None, progress=None):
    """Per-employee pay counters for every month this year (whole company -
    rosters filter later), cached for closed months like the leaderboards.
    Returns (months_dict, complete)."""
    co = COMPANIES[company]
    cache = _load_json(PAY_HISTORY_FILE, {})
    co_cache = cache.setdefault(company, {})
    today = local_today(co["tz"])
    current_key = f"{today.year:04d}-{today.month:02d}"
    complete = True
    result = {}
    for month in range(1, today.month + 1):
        key = f"{today.year:04d}-{month:02d}"
        entry = co_cache.get(key)
        if entry and key != current_key:
            month_end = dt.date(today.year + (month == 12), month % 12 + 1, 1)
            frozen = (today - month_end).days >= PAY_FREEZE_DAYS and entry.get("final")
            fresh = time.time() - entry.get("at", 0) < PAY_RECHECK_HOURS * 3600
            if frozen or fresh:
                result[key] = {int(k): v for k, v in entry["emps"].items()}
                continue
        if deadline and time.time() > deadline and key != current_key:
            complete = False
            if entry:
                result[key] = {int(k): v for k, v in entry["emps"].items()}
            continue
        t0 = time.time()
        first = dt.date(today.year, month, 1)
        last = dt.date(today.year + (month == 12), month % 12 + 1, 1) - dt.timedelta(days=1)
        if key == current_key:
            last = today
        emps = pay_window(co["tenant"], first, last)
        result[key] = emps
        if key != current_key:
            month_end = dt.date(today.year + (month == 12), month % 12 + 1, 1)
            rec = {"at": time.time(), "emps": emps,
                   "final": (today - month_end).days >= PAY_FREEZE_DAYS}
            update_history(PAY_HISTORY_FILE, company, key, rec)
        if progress:
            progress(company, f"pay {key}", time.time() - t0)
    return result, complete


# ------------------------------------------------------------------ goals
def _store_post(payload, timeout=15):
    req = urllib.request.Request(
        STORE_URL, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

def load_goals():
    """KPI goal table: committed defaults overridden by the Apps Script store
    (manager-editable once build/scorecard-goals.gs is deployed)."""
    goals = _load_json(GOALS_DEFAULTS_FILE, {}).get("kpi", {})
    try:
        resp = _store_post({"action": "getScorecardGoals"})
        if resp.get("ok") and isinstance(resp.get("goals"), dict):
            for role, table in resp["goals"].items():
                goals.setdefault(role, {}).update(table or {})
    except Exception:
        pass  # store not deployed yet / offline - defaults are fine
    return goals

def load_pay_targets():
    """{company: {employeeId(str): annual gross target}} - sensitive, only
    ever shipped inside the encrypted pay block."""
    pin = os.environ.get("SCORECARD_STORE_PIN")
    if pin:
        try:
            resp = _store_post({"action": "getScorecardPayTargets", "pin": pin})
            if resp.get("ok") and isinstance(resp.get("targets"), dict):
                return resp["targets"]
        except Exception:
            pass
    return _load_json(PAY_TARGETS_LOCAL, {})


# ------------------------------------------------------------------ encryption
def encrypt_pay(payload, passphrase):
    """AES-256-GCM blob the page decrypts with the manager passphrase via
    Web Crypto (PBKDF2-SHA256 key derivation). Returns None if the
    cryptography package is unavailable."""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
        from cryptography.hazmat.primitives import hashes
    except ImportError:
        return None
    salt, nonce = os.urandom(16), os.urandom(12)
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                     iterations=KDF_ITERATIONS).derive(passphrase.encode())
    ct = AESGCM(key).encrypt(nonce, json.dumps(payload).encode(), AAD)
    b64 = lambda b: base64.b64encode(b).decode()
    return {"v": 1, "kdf": "PBKDF2-SHA256", "iter": KDF_ITERATIONS,
            "salt": b64(salt), "nonce": b64(nonce), "ct": b64(ct),
            "aad": AAD.decode()}


# ------------------------------------------------------------------ assembly
def _board_rows(board):
    """{view: {company: {techId: row}}} from a sibling board's data.json."""
    path = os.path.join(ROOT, "site", board, "data.json")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    out = {}
    for view in ("mtd", "ytd"):
        out[view] = {c: {r["id"]: r for r in rows}
                     for c, rows in data["boards"][view].items() if c != "combined"}
    return out, data.get("complete", True)

def _ranks(rows_by_id, ids, key):
    order = sorted(ids, key=lambda i: -(rows_by_id.get(i, {}).get(key) or 0))
    n = len(order)
    return {tid: [order.index(tid) + 1, n] for tid in ids}

def compute(time_budget_secs=None, progress=None):
    """Full site/scorecard/data.json payload."""
    deadline = time.time() + time_budget_secs if time_budget_secs else None
    complete = True
    employees, periods_week = [], {}
    pay_secret = {"employees": {}, "targets": load_pay_targets()}

    # company pre-pass: pay period + pay feeds are role-independent, fetch once
    co_ctx = {}
    for company, co in COMPANIES.items():
        wk_from, wk_to = current_pay_period(company)
        periods_week[company] = {
            "from": wk_from.isoformat(), "to": wk_to.isoformat(),
            "label": f"{day_label(wk_from)} – {day_label(wk_to)}"}
        months, pay_ok = pay_months(company, deadline=deadline, progress=progress)
        complete = complete and pay_ok
        today = local_today(co["tz"])
        t0 = time.time()
        wk_pay = pay_window(co["tenant"], wk_from, min(wk_to, today))
        if progress:
            progress(company, "pay week", time.time() - t0)
        co_ctx[company] = {
            "wk_from": wk_from, "wk_to": wk_to, "months": months,
            "wk_pay": wk_pay,
            "current_key": f"{today.year:04d}-{today.month:02d}"}

    for role_key, role in ROLES.items():
        sibling, sib_complete = _board_rows(role["board"])
        complete = complete and sib_complete
        for company, co in COMPANIES.items():
            ctx = co_ctx[company]
            roster = role["roster"](company)
            t0 = time.time()
            week = role["week"](company, ctx["wk_from"], ctx["wk_to"], roster)
            if progress:
                progress(company, f"{role_key} week", time.time() - t0)

            mtd = sibling["mtd"].get(company, {})
            ytd = sibling["ytd"].get(company, {})
            rank_key = role["rankBy"]
            ranks = {"week": _ranks(week, list(roster), rank_key),
                     "mtd": _ranks(mtd, list(roster), rank_key),
                     "ytd": _ranks(ytd, list(roster), rank_key)}
            months, wk_pay, current_key = ctx["months"], ctx["wk_pay"], ctx["current_key"]

            for tid, info in roster.items():
                strip = lambda row: {k: v for k, v in (row or {}).items()
                                     if k not in ("id", "name", "team", "company",
                                                  "companyLabel", "color")}
                employees.append({
                    "id": tid, "name": info["name"], "team": info.get("team", ""),
                    "role": role_key, "company": company,
                    "companyLabel": co["label"], "color": co["color"],
                    "kpis": {"week": week.get(tid) or {},
                             "mtd": strip(mtd.get(tid)),
                             "ytd": strip(ytd.get(tid))},
                    "ranks": {v: ranks[v][tid] for v in ranks},
                })
                pay_secret["employees"][str(tid)] = {
                    "week": wk_pay.get(tid) or _new_pay(),
                    "mtd": months.get(current_key, {}).get(tid) or _new_pay(),
                    "ytd": _sum_pay([m.get(tid) or _new_pay()
                                     for m in months.values()]),
                }

    today = local_today("pacific")
    year_elapsed = ((today - dt.date(today.year, 1, 1)).days + 1) / \
                   (366 if today.year % 4 == 0 else 365)

    passphrase = os.environ.get("SCORECARD_KEY", "").strip()
    pay_enc, pay_note = None, ""
    if not passphrase:
        pay_note = ("Pay is not published: set the SCORECARD_KEY secret and "
                    "the pay section will appear, unlockable with that passphrase.")
    else:
        pay_enc = encrypt_pay(pay_secret, passphrase)
        if pay_enc is None:
            pay_note = ("Pay omitted: the 'cryptography' package is not "
                        "installed in this environment.")

    return {
        "updated": dt.datetime.now().strftime("%a %b %d %Y %H:%M:%S"),
        "complete": complete,
        "periods": {"week": periods_week,
                    "mtd": today.strftime("%B %Y"), "ytd": str(today.year)},
        "yearElapsed": round(year_elapsed, 4),
        "roles": {k: {"label": r["label"], "board": r["board"],
                      "rankBy": r["rankBy"], "metrics": r["metrics"]}
                  for k, r in ROLES.items()},
        "companies": {c: {"label": co["label"], "color": co["color"]}
                      for c, co in COMPANIES.items()},
        "goals": load_goals(),
        "employees": employees,
        "pay": pay_enc,
        "payNote": pay_note,
    }


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) > 2 and sys.argv[1] == "pay":
        company = sys.argv[2]
        co = COMPANIES[company]
        roster = ROLES["tech"]["roster"](company)
        today = local_today(co["tz"])
        pay = pay_window(co["tenant"], dt.date(today.year, today.month, 1), today)
        for tid, info in sorted(roster.items(), key=lambda kv: kv[1]["name"]):
            p = pay.get(tid) or _new_pay()
            types = " ".join(f"{t}={v:,.0f}" for t, v in sorted(p["byType"].items()))
            print(f"{info['name']:28s} gross {p['gross']:>10,.2f}  "
                  f"reg {p['regHours']:>6.1f}h ot {p['otHours']:>5.1f}h  {types}")
    else:
        t0 = time.time()
        data = compute(progress=lambda co, what, secs:
                       print(f"  {co} {what} in {secs:.1f}s", flush=True))
        n = len(data["employees"])
        wk = data["periods"]["week"]
        print(f"{n} employees; week windows: "
              + ", ".join(f"{c}={w['label']}" for c, w in wk.items()))
        print(f"pay: {'encrypted' if data['pay'] else 'omitted'} {data['payNote']}")
        print(f"complete={data['complete']} in {time.time() - t0:.0f}s")
