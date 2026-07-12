#!/usr/bin/env python3
"""
Live ServiceTitan engine for the HVAC Install Leaderboard.

Ranks HVAC install crews by revenue, MTD and YTD, for Sierra, Ultimate and
Russett (plus a combined board). Roster comes from People > Technicians
(active only), picked by team:

    sierra     team starts with "retro" (Retro + Retro QA)
    ultimate   team starts with "3", excluding duct cleaning (3/3A-3D Retro Install)
    russett    team starts with "install" (Install - Completion - AOR / Rough-In)

Per tech, per calendar month (summed across months for YTD):

  revenue        job.total of completed jobs, split by the payroll job-split %
                 (crew pairs share a job's splits; verified to reproduce the
                 install board reference screenshot exactly)
  jobs           completed jobs where the tech holds a split > 0
  avg install    revenue / revenue-bearing jobs (jobs with total > 0) -
                 warranty/QA/prep jobs don't dilute the average
  hours/job      paid on-job hours (payroll gross-pay-items of type
                 TimesheetTime with a jobId, driving included) / jobs
  overtime       paid hours with paidTimeType == Overtime; MTD per month and
                 WTD for the current payroll period (period detected from the
                 payrolls feed, Monday fallback)
  callbacks      recall jobs completed in the month whose ORIGINAL job
                 (recallForId) this tech held a split on - "your install got
                 called back", both crew members are charged
  warranty %     completed jobs carrying a warrantyId / completed jobs

Closed months are cached in data/install-board-history.json and recomputed at
most daily until they are 10 days past month-end, then frozen. The current
month is recomputed on every run.

CLI smoke test:
    py build/install_board_live.py                 # all companies, current month
    py build/install_board_live.py sierra 2026-06  # one company, one month
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
from servicetitan_client import st_get
from tech_board_live import (month_window_utc, _iso_z, _local_ts_utc,
                             clean_name)

HISTORY_FILE = os.path.join(ROOT, "data", "install-board-history.json")
SPLITS_BUFFER_DAYS = 60      # splits are created at assignment, before completion
MONTH_FREEZE_DAYS = 10       # closed month is final this long after month-end
MONTH_RECHECK_HOURS = 24     # until frozen, closed months refresh at most daily

COMPANIES = {
    "sierra": {
        "tenant": "SIE", "tz": "pacific", "label": "Sierra",
        "color": "#1663c7",
        "team_match": lambda t: t.startswith("retro"),
    },
    "ultimate": {
        "tenant": "ULT", "tz": "mountain", "label": "Ultimate",
        "color": "#c7161d",
        "team_match": lambda t: t.startswith("3") and "duct" not in t,
    },
    "russett": {
        "tenant": "RUS", "tz": "arizona", "label": "RSW",
        "color": "#0e7a3d",
        "team_match": lambda t: t.startswith("install"),
    },
}


# ---------------------------------------------------------------- roster
def team_technicians(company):
    """Active technicians on the install teams: {techId: {name, team}}."""
    co = COMPANIES[company]
    techs = fetch_all(co["tenant"], "/settings/v2/tenant/{tenant}/technicians",
                      {"active": "True"}, page_size=200)
    roster = {}
    for t in techs:
        team = re.sub(r"\s+", " ", (t.get("team") or "").strip().lower())
        if team and co["team_match"](team):
            roster[t["id"]] = {
                "id": t["id"],
                "name": clean_name(t.get("name")),
                "team": (t.get("team") or "").strip(),
            }
    return roster


# ---------------------------------------------------------------- payroll hours
def _month_local_days(tz, year, month):
    """(first_day_iso, last_day_iso) of the tenant-local calendar month."""
    first = dt.date(year, month, 1)
    last = dt.date(year + (month == 12), month % 12 + 1, 1) - dt.timedelta(days=1)
    return first.isoformat(), last.isoformat()

def pay_hours(tenant, day_from, day_to, roster):
    """{techId: {"jobHours": h, "otHours": h}} from payroll gross-pay-items.

    Item `date` is the shift day (a date-only timestamp), so the window is
    passed as plain dates. TimesheetTime items with a jobId are on-job time
    (working + driving); paidTimeType Overtime marks OT regardless of job.
    """
    out = {tid: {"jobHours": 0.0, "otHours": 0.0} for tid in roster}
    for i in fetch_all(tenant, "/payroll/v2/tenant/{tenant}/gross-pay-items",
                       {"dateOnOrAfter": day_from, "dateOnOrBefore": day_to},
                       page_size=500, max_pages=400):
        tid = i.get("employeeId")
        if tid not in out or i.get("grossPayItemType") != "TimesheetTime":
            continue
        hrs = float(i.get("paidDurationHours") or 0)
        if i.get("jobId"):
            out[tid]["jobHours"] += hrs
        if i.get("paidTimeType") == "Overtime":
            out[tid]["otHours"] += hrs
    return out


def current_period_start(company):
    """Tenant-local first day of the payroll period containing today.

    Detected from the payrolls feed (weekly for SIE/RUS, biweekly for ULT);
    falls back to the current Monday if nothing matches.
    """
    co = COMPANIES[company]
    today = local_today(co["tz"])
    now = dt.datetime.utcnow()
    lookback = _iso_z(dt.datetime(today.year, today.month, today.day)
                      - dt.timedelta(days=16))
    best = None
    try:
        for p in fetch_all(co["tenant"], "/payroll/v2/tenant/{tenant}/payrolls",
                           {"startedOnOrAfter": lookback}, page_size=200, max_pages=20):
            try:
                s = dt.datetime.strptime(p["startedOn"][:19], "%Y-%m-%dT%H:%M:%S")
                e = dt.datetime.strptime(p["endedOn"][:19], "%Y-%m-%dT%H:%M:%S")
            except (KeyError, TypeError, ValueError):
                continue
            if s <= now < e and (best is None or s > best):
                best = s
    except Exception as e:
        # WTD hours fall back to Monday-start; say so instead of hiding it.
        print(f"WARNING: {company} payroll-period lookup failed ({e}) - "
              f"using Monday fallback for WTD hours", flush=True)
    if best is not None:
        day = (best + dt.timedelta(hours=_utc_offset_hours(co["tz"], best.date()))).date()
        return min(day, today)
    return today - dt.timedelta(days=today.weekday())  # Monday fallback


# ---------------------------------------------------------------- month core
def _new_counters():
    return {"revenue": 0.0, "jobs": 0, "revJobs": 0, "jobHours": 0.0,
            "otHours": 0.0, "callbacks": 0, "warrJobs": 0}

def compute_month(company, year, month, roster=None):
    """Raw per-tech counters for one company and one calendar month."""
    co = COMPANIES[company]
    tenant, tz = co["tenant"], co["tz"]
    roster = roster if roster is not None else team_technicians(company)
    start, end = month_window_utc(tz, year, month)
    buf_splits = _iso_z(_local_ts_utc(tz, dt.date(year, month, 1))
                        - dt.timedelta(days=SPLITS_BUFFER_DAYS))

    # -- completed jobs in the month
    jobs = fetch_all(tenant, "/jpm/v2/tenant/{tenant}/jobs",
                     {"completedOnOrAfter": start, "completedBefore": end,
                      "jobStatus": "Completed"}, page_size=500, max_pages=200)
    jobs_by_id = {j["id"]: j for j in jobs}

    # -- payroll splits (tech attribution + revenue %)
    splits_by_job = {}
    for s in fetch_all(tenant, "/payroll/v2/tenant/{tenant}/jobs/splits",
                       {"createdOnOrAfter": buf_splits, "createdBefore": end},
                       page_size=500, max_pages=400):
        splits_by_job.setdefault(s["jobId"], []).append(s)

    def job_splits(jid):
        if jid not in splits_by_job:
            try:
                r = st_get(tenant, f"/payroll/v2/tenant/{{tenant}}/jobs/{jid}/splits")
                splits_by_job[jid] = r.get("data") or []
            except Exception:
                splits_by_job[jid] = []
        return splits_by_job[jid]

    missing = [j for j in jobs_by_id if j not in splits_by_job][:300]
    for jid in missing:  # rare (<0.5%): per-job fallback
        job_splits(jid)

    out = {tid: _new_counters() for tid in roster}

    # -- job-based metrics
    for job in jobs:
        total = float(job.get("total") or 0)
        for s in splits_by_job.get(job["id"], ()):
            tid = s["technicianId"]
            if tid not in out or (s.get("split") or 0) <= 0:
                continue
            c = out[tid]
            c["revenue"] += total * float(s["split"]) / 100.0
            c["jobs"] += 1
            c["revJobs"] += total > 0
            c["warrJobs"] += job.get("warrantyId") is not None

    # -- callbacks: recalls completed this month, charged to the original
    #    job's crew (both members of a split share the callback)
    recalls = [j for j in jobs if j.get("recallForId")]
    for rec in recalls:
        for s in job_splits(rec["recallForId"]):
            tid = s["technicianId"]
            if tid in out and (s.get("split") or 0) > 0:
                out[tid]["callbacks"] += 1

    # -- paid hours (on-job + overtime) for the month
    day_from, day_to = _month_local_days(tz, year, month)
    for tid, h in pay_hours(tenant, day_from, day_to, roster).items():
        out[tid]["jobHours"] = round(h["jobHours"], 2)
        out[tid]["otHours"] = round(h["otHours"], 2)

    for c in out.values():
        c["revenue"] = round(c["revenue"], 2)
    return out


# ---------------------------------------------------------------- caching
def _month_key(year, month):
    return f"{year:04d}-{month:02d}"

def months_of_year(company):
    today = local_today(COMPANIES[company]["tz"])
    return [(today.year, m) for m in range(1, today.month + 1)], today

def compute_company(company, deadline=None, progress=None):
    """Per-tech counters for every month this year, cached for closed months.
    Returns (months_dict, roster, complete)."""
    def _int_keys(techs):
        return {int(k): v for k, v in techs.items()}

    cache = _load_json(HISTORY_FILE, {})
    co_cache = cache.setdefault(company, {})
    months, today = months_of_year(company)
    roster = team_technicians(company)
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
                result[key] = _int_keys(entry["techs"])
                continue
        if deadline and time.time() > deadline and key != current_key:
            complete = False   # out of time - keep whatever cache we have
            if entry:
                result[key] = _int_keys(entry["techs"])
            continue
        t0 = time.time()
        techs = compute_month(company, year, month, roster=roster)
        result[key] = techs
        if key != current_key:
            month_end = dt.date(year + (month == 12), month % 12 + 1, 1)
            rec = {"at": time.time(), "techs": techs,
                   "final": (today - month_end).days >= MONTH_FREEZE_DAYS}
            update_history(HISTORY_FILE, company, key, rec)
        if progress:
            progress(company, key, time.time() - t0)
    return result, roster, complete


# ---------------------------------------------------------------- public API
def _finalize(counters):
    c = counters
    return {
        "revenue": round(c["revenue"], 2),
        "jobs": c["jobs"],
        "hoursPerJob": round(c["jobHours"] / c["jobs"], 2) if c["jobs"] else 0,
        "otHours": round(c["otHours"], 1),
        "callbacks": c["callbacks"],
        "warrantyPct": round(c["warrJobs"] / c["jobs"] * 100, 1) if c["jobs"] else 0,
        "avgInstall": round(c["revenue"] / c["revJobs"]) if c["revJobs"] else 0,
    }

def _sum_counters(dicts):
    total = _new_counters()
    for d in dicts:
        for k, v in d.items():
            total[k] += v
    return total

def compute(time_budget_secs=None, progress=None):
    """Full data.json payload for the dashboard."""
    deadline = time.time() + time_budget_secs if time_budget_secs else None
    boards = {"mtd": {}, "ytd": {}}
    complete = True

    def one(company):
        co = COMPANIES[company]
        per_month, roster, ok = compute_company(company, deadline=deadline, progress=progress)
        # week-to-date OT for the current payroll period (shown on both views)
        wtd_start = current_period_start(company)
        wtd = pay_hours(co["tenant"], wtd_start.isoformat(),
                        local_today(co["tz"]).isoformat(), roster)
        return per_month, roster, ok, wtd

    results = map_companies(one, COMPANIES)
    for company, co in COMPANIES.items():
        _, today = months_of_year(company)
        per_month, roster, ok, wtd = results[company]
        complete = complete and ok
        current_key = _month_key(today.year, today.month)

        rows_mtd, rows_ytd = [], []
        for tid, info in roster.items():
            base = {"id": tid, "name": info["name"], "team": info["team"],
                    "company": company, "companyLabel": co["label"], "color": co["color"],
                    "otWtd": round(wtd.get(tid, {}).get("otHours", 0), 1)}
            mtd = per_month.get(current_key, {}).get(tid) or _new_counters()
            ytd = _sum_counters([m.get(tid) or _new_counters() for m in per_month.values()])
            rows_mtd.append(dict(base, **_finalize(mtd)))
            rows_ytd.append(dict(base, **_finalize(ytd)))
        for rows, view in ((rows_mtd, "mtd"), (rows_ytd, "ytd")):
            rows.sort(key=lambda r: -r["revenue"])
            boards[view][company] = rows

    for view in ("mtd", "ytd"):
        combined = [r for c in COMPANIES for r in boards[view][c]]
        combined.sort(key=lambda r: -r["revenue"])
        boards[view]["combined"] = combined

    today = local_today("pacific")
    return {
        "updated": dt.datetime.now().strftime("%a %b %d %Y %H:%M:%S"),
        "complete": complete,
        "period": {"mtd": today.strftime("%B %Y"), "ytd": str(today.year)},
        "companies": {c: {"label": co["label"], "color": co["color"]}
                      for c, co in COMPANIES.items()},
        "boards": boards,
    }


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) > 1:
        company = sys.argv[1]
        ym = sys.argv[2] if len(sys.argv) > 2 else None
        if ym:
            y, m = map(int, ym.split("-"))
        else:
            t = local_today(COMPANIES[company]["tz"])
            y, m = t.year, t.month
        t0 = time.time()
        roster = team_technicians(company)
        techs = compute_month(company, y, m, roster=roster)
        rows = sorted(((c["revenue"], roster[tid]["name"], c) for tid, c in techs.items()),
                      reverse=True)
        for rev, name, c in rows:
            f = _finalize(c)
            print(f"{name:28s} rev {rev:>10,.0f} jobs {c['jobs']:>3} "
                  f"hrs/job {f['hoursPerJob']:>5.2f} ot {c['otHours']:>6.1f} "
                  f"cb {c['callbacks']:>2} warr {f['warrantyPct']:>5.1f}% "
                  f"avg {f['avgInstall']:>7,.0f}")
        print(f"-- {company} {y}-{m:02d} in {time.time() - t0:.1f}s", file=sys.stderr)
    else:
        print(json.dumps(compute(), indent=1)[:2000])
