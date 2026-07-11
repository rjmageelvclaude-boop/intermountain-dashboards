#!/usr/bin/env python3
"""
Live ServiceTitan engine for the Plumber Leaderboard.

Ranks active plumbing technicians by total revenue, MTD and YTD, for Sierra
and Ultimate (plus a combined board). Roster comes from People > Technicians
(active only), picked by team:

    sierra     team is exactly "Plumber - Sales Tech", "Plumber - Drain/Camera"
               or "Plumber - Maintenance"
    ultimate   team starts with "4a" / "4b" (4A - Plumbing, 4B - Plumbing
               Training)

Per tech, per calendar month (summed across months for YTD) the job/estimate
mechanics are identical to tech_board_live.py (revenue by payroll splits,
opportunity/conversion by job-type soldThreshold, sales by soldBy, membership
sold/conv/offered against customers with no membership active before the
visit), minus TGL, plus three product KPIs counted off sold estimates by the
selling tech (units and dollars):

  tankless / tanked / filtration
      line items on estimates SOLD in the month with soldBy == tech whose
      sku code/name is in PRODUCT_SKUS below.

Membership line items by tenant (sku name on the estimate item):
    SIE  SAM01..SAM12
    ULT  "Lion Shield New Membership" (exact; the free two-year promo item
         is deliberately not counted as a sale)

Sierra product skus are pricebook codes supplied by RJ. Ultimate's plumbing
pricebook shares none of them, so its sets were derived from every priced
water-heater/softener sale item on 2026 sold estimates - repairs, flushes,
diagnostics, $0 material lines and REME HALO air purifiers excluded. Adjust
PRODUCT_SKUS["ultimate"] as their pricebook evolves.

Closed months are cached in data/plumb-board-history.json and recomputed at
most daily until they are 10 days past month-end, then frozen. The current
month is recomputed on every run.

CLI smoke test:
    py build/plumb_board_live.py                  # all companies, current month
    py build/plumb_board_live.py sierra 2026-06   # one company, one month
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
from command_center_live import fetch_all, local_today, _load_json, _save_json
from servicetitan_client import st_get
from tech_board_live import (DEFAULT_SOLD_THRESHOLD, ESTIMATE_BUFFER_DAYS,
                             MONTH_FREEZE_DAYS, MONTH_RECHECK_HOURS,
                             SPLITS_BUFFER_DAYS, _iso_z, _local_completed_day,
                             _local_ts_utc, _member_before,
                             _memberships_for_customers, clean_name,
                             month_window_utc, sold_thresholds)

HISTORY_FILE = os.path.join(ROOT, "data", "plumb-board-history.json")

COMPANIES = {
    "sierra": {
        "tenant": "SIE", "tz": "pacific", "label": "Sierra",
        "color": "#1663c7",
        "team_match": lambda t: t in ("plumber - sales tech",
                                      "plumber - drain/camera",
                                      "plumber - maintenance"),
    },
    "ultimate": {
        "tenant": "ULT", "tz": "mountain", "label": "Ultimate",
        "color": "#c7161d",
        "team_match": lambda t: t.startswith(("4a", "4b")),
    },
}

_SAM = re.compile(r"^sam(0[1-9]|1[0-2])$")

def _is_memb_sku(company, name):
    n = (name or "").strip().lower()
    if not n:
        return False
    if company == "sierra":
        return bool(_SAM.match(n))
    if company == "ultimate":
        return n == "lion shield new membership"
    return False


PRODUCT_SKUS = {
    "sierra": {
        "tankless": {"whnavi01", "whn10", "whn02", "whn03", "whess01",
                     "whtru01"},
        "tanked": {"whb04", "whb05", "whb01", "whb09", "whbw01", "whb07",
                   "whb13", "whb08", "whb06", "whb02", "whb10", "whbw02",
                   "whb03", "whb11", "whw03"},
        "filtration": {"wt07", "wd01", "wtf02", "wt30", "wt11", "wt03",
                       "wt04", "wt02", "wt05", "wt01"},
    },
    "ultimate": {
        "tankless": {"whtl-100", "ntwh210s2", "tankless water heater install"},
        "tanked": {"50 gal water heater replacement",
                   "40 gal water heater replacement",
                   "whng-100", "whng-120", "whng-130", "whng-190",
                   "whe-110", "whe-120", "e-wh-res-el-050"},
        "filtration": {"water softener halo", "nwscu", "nws48k", "cs-210",
                       "wtisis-100", "wtifipou-185", "nwsl"},
    },
}
PRODUCT_KEYS = ("tankless", "tanked", "filtration")

def _product_key(company, name):
    n = re.sub(r"\s+", " ", (name or "").strip().lower())
    if not n:
        return None
    for key in PRODUCT_KEYS:
        if n in PRODUCT_SKUS[company][key]:
            return key
    return None


# ---------------------------------------------------------------- roster
def team_technicians(company):
    """Active technicians on the board's teams: {techId: {name, team, ids}}."""
    co = COMPANIES[company]
    techs = fetch_all(co["tenant"], "/settings/v2/tenant/{tenant}/technicians",
                      {"active": "True"}, page_size=200)
    roster = {}
    for t in techs:
        team = re.sub(r"\s+", " ", (t.get("team") or "").strip().lower())
        if team and co["team_match"](team):
            roster[t["id"]] = {
                "id": t["id"],
                "userId": t.get("userId"),
                "name": clean_name(t.get("name")),
                "team": (t.get("team") or "").strip(),
            }
    return roster


# ---------------------------------------------------------------- month core
def _new_counters():
    c = {"revenue": 0.0, "jobs": 0, "opps": 0, "converted": 0,
         "sales": 0.0, "membSold": 0, "nonMemberJobs": 0,
         "nonMemberSold": 0, "nonMemberOffered": 0}
    for key in PRODUCT_KEYS:
        c[key + "N"] = 0
        c[key + "Amt"] = 0.0
    return c

def compute_month(company, year, month, roster=None, thresholds=None):
    """Raw per-tech counters for one company and one calendar month."""
    co = COMPANIES[company]
    tenant, tz = co["tenant"], co["tz"]
    roster = roster if roster is not None else team_technicians(company)
    thresholds = thresholds if thresholds is not None else sold_thresholds(tenant)
    start, end = month_window_utc(tz, year, month)
    buf_splits = _iso_z(_local_ts_utc(tz, dt.date(year, month, 1))
                        - dt.timedelta(days=SPLITS_BUFFER_DAYS))
    buf_est = _iso_z(_local_ts_utc(tz, dt.date(year, month, 1))
                     - dt.timedelta(days=ESTIMATE_BUFFER_DAYS))

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
    missing = [j for j in jobs_by_id if j not in splits_by_job][:300]
    for jid in missing:  # rare (<0.5%): per-job fallback
        try:
            r = st_get(tenant, f"/payroll/v2/tenant/{{tenant}}/jobs/{jid}/splits")
            if r.get("data"):
                splits_by_job[jid] = r["data"]
        except Exception:
            pass

    # -- estimates: created near the month (offered-rate join by job) and
    #    sold inside the month (sales, memberships, products)
    est_created = fetch_all(tenant, "/sales/v2/tenant/{tenant}/estimates",
                            {"createdOnOrAfter": buf_est, "createdBefore": end},
                            page_size=500, max_pages=200)
    est_sold = [e for e in fetch_all(
        tenant, "/sales/v2/tenant/{tenant}/estimates",
        {"soldAfter": start, "soldBefore": end}, page_size=500, max_pages=200)
        if ((e.get("status") or {}).get("name") or "") != "Dismissed"]

    def memb_qty(est):
        return sum(max(1, int(float(it.get("qty") or 0)))
                   for it in (est.get("items") or [])
                   if _is_memb_sku(company, (it.get("sku") or {}).get("name")))

    ests_on_job, sold_on_job = {}, {}
    for e in est_created:
        if e.get("jobId"):
            ests_on_job.setdefault(e["jobId"], []).append(e)
    for e in est_sold:
        if e.get("jobId"):
            sold_on_job.setdefault(e["jobId"], []).append(e)

    # -- membership status for customers of the roster's completed jobs
    team_jobs = []
    for j in jobs:
        techs = [s for s in splits_by_job.get(j["id"], ())
                 if s["technicianId"] in roster and (s.get("split") or 0) > 0]
        if techs:
            team_jobs.append((j, techs))
    memberships = _memberships_for_customers(
        tenant, {j["customerId"] for j, _ in team_jobs if j.get("customerId")})

    tech_key = {}
    for t in roster.values():
        tech_key[t["id"]] = t["id"]
        if t.get("userId"):
            tech_key[t["userId"]] = t["id"]

    out = {tid: _new_counters() for tid in roster}

    # -- job-based metrics
    for job, techs in team_jobs:
        total = float(job.get("total") or 0)
        thr = thresholds.get(job.get("jobTypeId"), DEFAULT_SOLD_THRESHOLD)
        opp = (not job.get("noCharge")) or total >= thr
        conv = opp and total >= thr
        day = _local_completed_day(tz, job["completedOn"]).isoformat()
        non_member = (job.get("customerId") is not None
                      and not _member_before(memberships, job["customerId"], day))
        offered = any(memb_qty(e) for e in ests_on_job.get(job["id"], ()))
        sold_memb = sum(memb_qty(e) for e in sold_on_job.get(job["id"], ()))
        for s in techs:
            c = out[s["technicianId"]]
            c["revenue"] += total * float(s.get("split") or 0) / 100.0
            c["jobs"] += 1
            c["opps"] += opp
            c["converted"] += conv
            c["membSold"] += sold_memb
            if non_member:
                c["nonMemberJobs"] += 1
                c["nonMemberOffered"] += bool(offered or sold_memb)
                c["nonMemberSold"] += bool(sold_memb)

    # -- estimate-based metrics (sold-by): total sales + product units/dollars
    for e in est_sold:
        seller = tech_key.get(e.get("soldBy"))
        if not seller:
            continue
        c = out[seller]
        c["sales"] += float(e.get("subtotal") or 0)
        for it in (e.get("items") or []):
            key = _product_key(company, (it.get("sku") or {}).get("name"))
            if key:
                c[key + "N"] += max(1, int(float(it.get("qty") or 0)))
                c[key + "Amt"] += float(it.get("total") or 0)

    for c in out.values():
        c["revenue"] = round(c["revenue"], 2)
        c["sales"] = round(c["sales"], 2)
        for key in PRODUCT_KEYS:
            c[key + "Amt"] = round(c[key + "Amt"], 2)
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
    thresholds = sold_thresholds(COMPANIES[company]["tenant"])
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
        techs = compute_month(company, year, month, roster=roster, thresholds=thresholds)
        result[key] = techs
        if key != current_key:
            month_end = dt.date(year + (month == 12), month % 12 + 1, 1)
            co_cache[key] = {"at": time.time(), "techs": techs,
                             "final": (today - month_end).days >= MONTH_FREEZE_DAYS}
            cache[company] = co_cache
            _save_json(HISTORY_FILE, cache)
        if progress:
            progress(company, key, time.time() - t0)
    return result, roster, complete


# ---------------------------------------------------------------- public API
def _finalize(counters):
    c = counters
    row = {
        "revenue": round(c["revenue"], 2),
        "sales": round(c["sales"], 2),
        "jobs": c["jobs"],
        "opps": c["opps"],
        "avgTicket": round(c["revenue"] / c["opps"]) if c["opps"] else 0,
        "convRate": round(c["converted"] / c["opps"] * 100, 1) if c["opps"] else 0,
        "membSold": c["membSold"],
        "membConv": round(c["nonMemberSold"] / c["nonMemberJobs"] * 100, 1) if c["nonMemberJobs"] else 0,
        "membOffered": round(c["nonMemberOffered"] / c["nonMemberJobs"] * 100, 1) if c["nonMemberJobs"] else 0,
        "nonMemberJobs": c["nonMemberJobs"],
    }
    for key in PRODUCT_KEYS:
        row[key + "N"] = c[key + "N"]
        row[key + "Amt"] = round(c[key + "Amt"], 2)
    return row

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
    for company, co in COMPANIES.items():
        months, today = months_of_year(company)
        per_month, roster, ok = compute_company(company, deadline=deadline, progress=progress)
        complete = complete and ok
        current_key = _month_key(today.year, today.month)

        rows_mtd, rows_ytd = [], []
        for tid, info in roster.items():
            base = {"id": tid, "name": info["name"], "team": info["team"],
                    "company": company, "companyLabel": co["label"], "color": co["color"]}
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
        techs = compute_month(company, y, m)
        rows = sorted(((c["revenue"], roster[tid]["name"], c) for tid, c in techs.items()
                       if tid in roster), reverse=True)
        for rev, name, c in rows:
            print(f"{name:28s} rev {rev:>10,.0f} jobs {c['jobs']:>3} opps {c['opps']:>3} "
                  f"conv {c['converted']:>3} sales {c['sales']:>9,.0f} memb {c['membSold']:>2} "
                  f"nonMem {c['nonMemberJobs']:>3} off {c['nonMemberOffered']:>3} "
                  f"tkls {c['tanklessN']}/${c['tanklessAmt']:,.0f} "
                  f"tank {c['tankedN']}/${c['tankedAmt']:,.0f} "
                  f"filt {c['filtrationN']}/${c['filtrationAmt']:,.0f}")
        print(f"-- {company} {y}-{m:02d} in {time.time() - t0:.1f}s", file=sys.stderr)
    else:
        print(json.dumps(compute(), indent=1)[:2000])
