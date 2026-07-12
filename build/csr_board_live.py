#!/usr/bin/env python3
"""
Live ServiceTitan engine for the CSR / Call Center Leaderboard.

Ranks call-center staff by inbound calls handled - Daily, MTD and YTD - for
Sierra, Ultimate and Russett (plus a combined board). Roster comes from
Settings > Employees (active only, any office role); the answering service
and non-person accounts are dropped by name (see EXCLUDE_NAME). A person
only appears on the board once they have call activity in the view's window.

Per agent (the telecom call's `agent`, falling back to `createdBy` for
outbound), calls deduped by leadCall id:

  inbound         inbound calls answered by the agent (any call type)
  outbound        outbound calls placed by the agent
  lead calls      inbound with callType Booked or Unbooked (same lead
                  definition as the Command Center booking rate)
  booked          inbound with callType Booked; booking rate = booked / leads
  email capture   booked calls whose customer record has an email on file /
                  booked calls (customer missing counts as no email)
  mem sold        memberships created in the window with soldById == agent
                  (over-the-phone membership sales credit the CSR directly)
  avg in / out    average call duration in minutes, split by direction
  contacts/hour   (inbound + outbound) / active hours, where an active hour
                  is a distinct tenant-local clock hour in which the agent
                  handled at least one call - a live proxy for time on the
                  phones that needs no timeclock feed

Closed months are cached in data/csr-board-history.json and recomputed at
most daily until they are 10 days past month-end, then frozen. The current
month and today's daily board are recomputed on every run.

CLI smoke test:
    py build/csr_board_live.py                 # all companies, current month
    py build/csr_board_live.py sierra 2026-07  # one company, one month
    py build/csr_board_live.py sierra today    # one company, today only
"""
import datetime as dt
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from command_center_live import (fetch_all, local_today, _load_json,
                                 _save_json, _utc_offset_hours,
                                 map_companies, update_history)

HISTORY_FILE = os.path.join(ROOT, "data", "csr-board-history.json")
MONTH_FREEZE_DAYS = 10       # closed month is final this long after month-end
MONTH_RECHECK_HOURS = 24     # until frozen, closed months refresh at most daily
LEAD_TYPES = ("Booked", "Unbooked", "NotBooked")

COMPANIES = {
    "sierra":   {"tenant": "SIE", "tz": "pacific",  "label": "Sierra",   "color": "#1663c7"},
    "ultimate": {"tenant": "ULT", "tz": "mountain", "label": "Ultimate", "color": "#c7161d"},
    "russett":  {"tenant": "RUS", "tz": "arizona",  "label": "Russett",  "color": "#0e7a3d"},
}

# Non-person accounts and the after-hours answering service (AVOCA) hold real
# employee records and answer real calls - keep them off the leaderboard.
EXCLUDE_NAME = re.compile(
    r"avoca|abandon|customer service email|automation|display|- reports?\b|"
    r"sierraairconditioning|russett? ?air|ultimate ?heating", re.I)
EXCLUDE_ROLES = ("Owner", "DisplayUser")


# ---------------------------------------------------------------- utilities
def _iso_z(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")

def _local_midnight_utc(tz, day):
    off = _utc_offset_hours(tz, day)
    return dt.datetime(day.year, day.month, day.day) - dt.timedelta(hours=off)

def month_window_utc(tz, year, month):
    """UTC [start, end) covering the tenant-local calendar month."""
    first = dt.date(year, month, 1)
    nxt = dt.date(year + (month == 12), month % 12 + 1, 1)
    return _iso_z(_local_midnight_utc(tz, first)), _iso_z(_local_midnight_utc(tz, nxt))

def day_window_utc(tz, day):
    start = _local_midnight_utc(tz, day)
    return _iso_z(start), _iso_z(start + dt.timedelta(days=1))

def _duration_secs(s):
    """'00:03:00' or '1.02:03:04(.frac)' -> seconds."""
    if not s:
        return 0
    days = 0
    if "." in s.split(":")[0]:
        d, s = s.split(".", 1)
        days = int(d)
    parts = s.split(":")
    try:
        h, m = int(parts[0]), int(parts[1])
        sec = float(parts[2]) if len(parts) > 2 else 0
    except (ValueError, IndexError):
        return 0
    return days * 86400 + h * 3600 + m * 60 + sec

def _local_day_hour(tz, ts):
    """Tenant-local (isoday, hour) a UTC timestamp falls on."""
    t = dt.datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")
    t += dt.timedelta(hours=_utc_offset_hours(tz, t.date()))
    return t.date().isoformat(), t.hour


# ---------------------------------------------------------------- roster
def board_employees(company):
    """Active office employees eligible for the board: {empId: {name, role}}."""
    co = COMPANIES[company]
    emps = fetch_all(co["tenant"], "/settings/v2/tenant/{tenant}/employees",
                     {"active": "True"}, page_size=200)
    roster = {}
    for e in emps:
        name = re.sub(r"\s+", " ", (e.get("name") or "").strip())
        if not name or EXCLUDE_NAME.search(name) or e.get("role") in EXCLUDE_ROLES:
            continue
        roster[e["id"]] = {"id": e["id"], "name": name, "role": e.get("role") or ""}
    return roster


# ---------------------------------------------------------------- window core
def _new_counters():
    return {"inbound": 0, "outbound": 0, "leadCalls": 0, "booked": 0,
            "bookedEmail": 0, "inSecs": 0.0, "outSecs": 0.0,
            "membSold": 0, "hours": 0}

def compute_window(company, start, end, roster=None):
    """Raw per-agent counters for one UTC [start, end) window."""
    co = COMPANIES[company]
    tenant, tz = co["tenant"], co["tz"]
    roster = roster if roster is not None else board_employees(company)

    calls = fetch_all(tenant, "/telecom/v2/tenant/{tenant}/calls",
                      {"createdOnOrAfter": start, "createdBefore": end},
                      page_size=500, max_pages=400)

    out = {}
    hour_sets = {}
    seen = set()
    for c in calls:
        lc = c.get("leadCall") or {}
        cid = lc.get("id")
        if not cid or cid in seen:
            continue
        seen.add(cid)
        agent = (lc.get("agent") or {}).get("id") or (lc.get("createdBy") or {}).get("id")
        if agent not in roster:
            continue
        cnt = out.setdefault(agent, _new_counters())
        secs = _duration_secs(lc.get("duration"))
        if lc.get("direction") == "Inbound":
            cnt["inbound"] += 1
            cnt["inSecs"] += secs
            ct = lc.get("callType")
            if ct in LEAD_TYPES:
                cnt["leadCalls"] += 1
            if ct == "Booked":
                cnt["booked"] += 1
                if ((lc.get("customer") or {}).get("email") or "").strip():
                    cnt["bookedEmail"] += 1
        elif lc.get("direction") == "Outbound":
            cnt["outbound"] += 1
            cnt["outSecs"] += secs
        else:
            continue
        ts = lc.get("receivedOn") or lc.get("createdOn")
        if ts:
            hour_sets.setdefault(agent, set()).add(_local_day_hour(tz, ts))
    for agent, hours in hour_sets.items():
        out[agent]["hours"] = len(hours)

    # memberships sold over the phone credit the CSR via soldById
    for m in fetch_all(tenant, "/memberships/v2/tenant/{tenant}/memberships",
                       {"createdOnOrAfter": start, "createdBefore": end},
                       page_size=200, max_pages=200):
        seller = m.get("soldById")
        if seller in roster:
            out.setdefault(seller, _new_counters())["membSold"] += 1

    for cnt in out.values():
        cnt["inSecs"] = round(cnt["inSecs"], 1)
        cnt["outSecs"] = round(cnt["outSecs"], 1)
    return out

def compute_month(company, year, month, roster=None):
    start, end = month_window_utc(COMPANIES[company]["tz"], year, month)
    return compute_window(company, start, end, roster=roster)

def compute_today(company, roster=None):
    tz = COMPANIES[company]["tz"]
    start, end = day_window_utc(tz, local_today(tz))
    return compute_window(company, start, end, roster=roster)


# ---------------------------------------------------------------- caching
def _month_key(year, month):
    return f"{year:04d}-{month:02d}"

def months_of_year(company):
    today = local_today(COMPANIES[company]["tz"])
    return [(today.year, m) for m in range(1, today.month + 1)], today

def compute_company(company, deadline=None, progress=None):
    """Per-agent counters for every month this year, cached for closed months.
    Returns (months_dict, roster, complete)."""
    def _int_keys(agents):
        return {int(k): v for k, v in agents.items()}

    cache = _load_json(HISTORY_FILE, {})
    co_cache = cache.setdefault(company, {})
    months, today = months_of_year(company)
    roster = board_employees(company)
    current_key = _month_key(today.year, today.month)
    complete = True

    result = {}
    for year, month in months:
        key = _month_key(year, month)
        entry = co_cache.get(key)
        if entry and key != current_key:
            month_end = dt.date(year + (month == 12), month % 12 + 1, 1)
            frozen = (today - month_end).days >= MONTH_FREEZE_DAYS and entry.get("final")
            fresh = time.time() - entry.get("at", 0) < MONTH_RECHECK_HOURS * 3600
            if frozen or fresh:
                result[key] = _int_keys(entry["agents"])
                continue
        if deadline and time.time() > deadline and key != current_key:
            complete = False   # out of time - keep whatever cache we have
            if entry:
                result[key] = _int_keys(entry["agents"])
            continue
        t0 = time.time()
        agents = compute_month(company, year, month, roster=roster)
        result[key] = agents
        if key != current_key:
            month_end = dt.date(year + (month == 12), month % 12 + 1, 1)
            rec = {"at": time.time(), "agents": agents,
                   "final": (today - month_end).days >= MONTH_FREEZE_DAYS}
            update_history(HISTORY_FILE, company, key, rec)
        if progress:
            progress(company, key, time.time() - t0)
    return result, roster, complete


# ---------------------------------------------------------------- public API
def _finalize(c):
    contacts = c["inbound"] + c["outbound"]
    return {
        "inbound": c["inbound"],
        "outbound": c["outbound"],
        "leadCalls": c["leadCalls"],
        "booked": c["booked"],
        "bookingRate": round(c["booked"] / c["leadCalls"] * 100, 1) if c["leadCalls"] else 0,
        "emailCapture": round(c["bookedEmail"] / c["booked"] * 100, 1) if c["booked"] else 0,
        "membSold": c["membSold"],
        "avgInMins": round(c["inSecs"] / c["inbound"] / 60) if c["inbound"] else 0,
        "avgOutMins": round(c["outSecs"] / c["outbound"] / 60) if c["outbound"] else 0,
        "contactsPerHour": round(contacts / c["hours"], 1) if c["hours"] else 0,
        "contacts": contacts,
    }

def _sum_counters(dicts):
    total = _new_counters()
    for d in dicts:
        for k, v in d.items():
            total[k] += v
    return total

def _rows(per_agent, roster, base_of):
    rows = []
    for aid, c in per_agent.items():
        info = roster.get(aid)
        if not info or not (c["inbound"] or c["outbound"] or c["membSold"]):
            continue
        rows.append(dict(base_of(info), **_finalize(c)))
    rows.sort(key=lambda r: (-r["inbound"], -r["contacts"]))
    return rows

def compute(time_budget_secs=None, progress=None):
    """Full data.json payload for the dashboard."""
    deadline = time.time() + time_budget_secs if time_budget_secs else None
    boards = {"daily": {}, "mtd": {}, "ytd": {}}
    complete = True

    def one(company):
        per_month, roster, ok = compute_company(company, deadline=deadline, progress=progress)
        daily = compute_today(company, roster=roster)
        if progress:
            progress(company, "today", 0)
        return per_month, roster, ok, daily

    results = map_companies(one, COMPANIES)
    for company, co in COMPANIES.items():
        _, today = months_of_year(company)
        per_month, roster, ok, daily = results[company]
        complete = complete and ok
        current_key = _month_key(today.year, today.month)

        def base_of(info):
            return {"id": info["id"], "name": info["name"], "role": info["role"],
                    "company": company, "companyLabel": co["label"], "color": co["color"]}

        ytd = {}
        for m in per_month.values():
            for aid, c in m.items():
                ytd[aid] = _sum_counters([ytd[aid], c]) if aid in ytd else dict(c)

        boards["daily"][company] = _rows(daily, roster, base_of)
        boards["mtd"][company] = _rows(per_month.get(current_key, {}), roster, base_of)
        boards["ytd"][company] = _rows(ytd, roster, base_of)

    for view in boards:
        combined = [r for c in COMPANIES for r in boards[view][c]]
        combined.sort(key=lambda r: (-r["inbound"], -r["contacts"]))
        boards[view]["combined"] = combined

    today = local_today("pacific")
    return {
        "updated": dt.datetime.now().strftime("%a %b %d %Y %H:%M:%S"),
        "complete": complete,
        "period": {"daily": today.strftime("%a %b %d"),
                   "mtd": today.strftime("%B %Y"), "ytd": str(today.year)},
        "companies": {c: {"label": co["label"], "color": co["color"]}
                      for c, co in COMPANIES.items()},
        "boards": boards,
    }


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) > 1:
        company = sys.argv[1]
        arg = sys.argv[2] if len(sys.argv) > 2 else None
        roster = board_employees(company)
        t0 = time.time()
        if arg == "today":
            agents = compute_today(company, roster=roster)
        else:
            if arg:
                y, m = map(int, arg.split("-"))
            else:
                t = local_today(COMPANIES[company]["tz"])
                y, m = t.year, t.month
            agents = compute_month(company, y, m, roster=roster)
        rows = sorted(((c["inbound"], roster[a]["name"], c) for a, c in agents.items()
                       if a in roster and (c["inbound"] or c["outbound"] or c["membSold"])),
                      reverse=True)
        for inb, name, c in rows:
            f = _finalize(c)
            print(f"{name:26s} in {inb:>4} out {c['outbound']:>4} leads {c['leadCalls']:>4} "
                  f"bkd {c['booked']:>4} rate {f['bookingRate']:>5.1f}% email {f['emailCapture']:>5.1f}% "
                  f"memb {c['membSold']:>2} cph {f['contactsPerHour']:>4.1f} "
                  f"avgIn {f['avgInMins']:>2}m avgOut {f['avgOutMins']:>2}m hrs {c['hours']:>3}")
        print(f"-- {company} {arg or 'current month'} in {time.time() - t0:.1f}s", file=sys.stderr)
    else:
        print(json.dumps(compute(), indent=1)[:2000])
