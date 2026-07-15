#!/usr/bin/env python3
"""
Live ServiceTitan engine for the Install Callback Board.

How often does the HVAC install department go back to a finished install for
recall or finish work? Callbacks land anywhere from 1 to 180+ days after the
install, so a plain monthly rate is misleading - young months always look
better than old ones. The board therefore uses install-month COHORTS: each
month's installs are tracked for callbacks within 30/60/90/180 days, and a
window is only marked "final" once every install in the cohort has had that
long to fail. Immature windows still show their running value, flagged.

Scope: the HVAC install business unit of each company.

    sierra     SIE  BU 337        HVAC - Install - AOR
    ultimate   ULT  BU 12932      HVAC - Install - AOR
    russett    RUS  BU 42371009   HVAC - Install - AOR
    brothers   BRO  BU 2218902    HVAC Install

Job classification (by job-type name; jpm/v2 ignores businessUnitIds /
jobTypeIds query params server-side, so everything filters client-side):

    recall    type contains "recall"  (Recall/Warranty, Recall Install)
    finish    Retro Finish / Startup / Client Resolution - the crew returns
              to finish or commission work it couldn't complete
    qa        Quality Assurance / QA Crew Check - EXCLUDED (planned visit)
    drywall   Sierra Drywall - EXCLUDED (planned visit)
    install   everything else; completed + revenue-bearing = cohort member

A callback is linked to the install it belongs to by, in order:
recallForId -> same project (latest install completed on or before the
callback was created) -> same location (same rule). Callbacks whose install
predates the 18-month window stay unlinked: they count as workload in the
monthly trend but join no cohort.

Closed months are cached in data/callback-board-history.json, recomputed at
most daily until 40 days past month-end, then frozen. Current month always
recomputes. Rolling window: current + 18 closed months.

CLI smoke test:
    py build/callback_board_live.py                # summary, all companies
    py build/callback_board_live.py sierra 2026-06 # one company-month, raw
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
                                 map_companies, update_history)
from tech_board_live import month_window_utc

HISTORY_FILE = os.path.join(ROOT, "data", "callback-board-history.json")
WINDOW_CLOSED_MONTHS = 18    # cohort months kept besides the current month
MONTH_FREEZE_DAYS = 40       # month is final this long after month-end
MONTH_RECHECK_HOURS = 24     # until frozen, closed months refresh at most daily
OPEN_CB_LOOKBACK_DAYS = 75   # created-window scanned for still-open callbacks
WINDOWS = (30, 60, 90, 180)
RECENT_LIMIT = 60            # rows in the recent-callbacks table per company

COMPANIES = {
    "sierra":   {"tenant": "SIE", "tz": "pacific",  "label": "Sierra",
                 "color": "#1663c7", "bu": 337},
    "ultimate": {"tenant": "ULT", "tz": "mountain", "label": "Ultimate",
                 "color": "#c7161d", "bu": 12932},
    "russett":  {"tenant": "RUS", "tz": "arizona",  "label": "RSW",
                 "color": "#0e7a3d", "bu": 42371009},
    "brothers": {"tenant": "BRO", "tz": "mountain", "label": "Brothers",
                 "color": "#c2410c", "bu": 2218902},
}

RE_QA = re.compile(r"quality assurance|qa crew|q/a", re.I)
RE_DRYWALL = re.compile(r"drywall", re.I)
RE_RECALL = re.compile(r"recall", re.I)
RE_FINISH = re.compile(r"retro finish|^startup$|client resolution", re.I)


def classify(type_name):
    n = (type_name or "").strip()
    if RE_QA.search(n):
        return "qa"
    if RE_DRYWALL.search(n):
        return "drywall"
    if RE_RECALL.search(n):
        return "recall"
    if RE_FINISH.search(n):
        return "finish"
    return "install"


# ---------------------------------------------------------------- fetch
_JOB_TYPES = {}

def job_types(tenant):
    """{jobTypeId: name}, including inactive types (fetched on miss)."""
    if tenant not in _JOB_TYPES:
        _JOB_TYPES[tenant] = {t["id"]: t.get("name") or "" for t in fetch_all(
            tenant, "/jpm/v2/tenant/{tenant}/job-types", {}, page_size=200)}
    return _JOB_TYPES[tenant]


def _day(ts):
    return ts[:10] if ts else None


def month_events(company, year, month):
    """Raw install/callback events for jobs COMPLETED in the month.

    {"installs": [{i,d,loc,proj,t}], "callbacks": [...], "qa": n, "drywall": n}
    Dates are ISO days; classification happens here so frozen months never
    need the job-types lookup again.
    """
    co = COMPANIES[company]
    start, end = month_window_utc(co["tz"], year, month)
    jt = job_types(co["tenant"])
    installs, callbacks = [], []
    qa = drywall = 0
    for j in fetch_all(co["tenant"], "/jpm/v2/tenant/{tenant}/jobs",
                       {"completedOnOrAfter": start, "completedBefore": end,
                        "jobStatus": "Completed"},
                       page_size=500, max_pages=400):
        if j.get("businessUnitId") != co["bu"]:
            continue
        cat = classify(jt.get(j.get("jobTypeId")))
        if cat == "qa":
            qa += 1
        elif cat == "drywall":
            drywall += 1
        elif cat in ("recall", "finish"):
            callbacks.append({
                "i": j["id"], "cat": cat,
                "ty": jt.get(j.get("jobTypeId")) or "?",
                "d": _day(j.get("completedOn")),
                "c": _day(j.get("createdOn")),
                "loc": j.get("locationId"), "proj": j.get("projectId"),
                "rf": j.get("recallForId"),
            })
        elif float(j.get("total") or 0) > 0:
            installs.append({
                "i": j["id"], "d": _day(j.get("completedOn")),
                "loc": j.get("locationId"), "proj": j.get("projectId"),
                "t": round(float(j["total"]), 2),
            })
    return {"installs": installs, "callbacks": callbacks,
            "qa": qa, "drywall": drywall}


def open_callbacks(company):
    """Recall/finish jobs booked but not yet completed (created in the last
    OPEN_CB_LOOKBACK_DAYS - callbacks are booked days, not months, ahead)."""
    co = COMPANIES[company]
    since = (dt.datetime.utcnow()
             - dt.timedelta(days=OPEN_CB_LOOKBACK_DAYS)).strftime(
                 "%Y-%m-%dT00:00:00Z")
    jt = job_types(co["tenant"])
    n = 0
    for j in fetch_all(co["tenant"], "/jpm/v2/tenant/{tenant}/jobs",
                       {"createdOnOrAfter": since}, page_size=500,
                       max_pages=400):
        if (j.get("businessUnitId") == co["bu"]
                and j.get("jobStatus") not in ("Completed", "Canceled")
                and classify(jt.get(j.get("jobTypeId"))) in ("recall", "finish")):
            n += 1
    return n


# ---------------------------------------------------------------- caching
def _month_key(year, month):
    return f"{year:04d}-{month:02d}"


def window_months(company):
    """[(y, m)] oldest->newest: 18 closed months + the current month."""
    today = local_today(COMPANIES[company]["tz"])
    y, m = today.year, today.month
    out = []
    for _ in range(WINDOW_CLOSED_MONTHS + 1):
        out.append((y, m))
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return list(reversed(out)), today


def compute_company(company, deadline=None, progress=None):
    """{month_key: events} across the window, cached. Returns (months, complete)."""
    cache = _load_json(HISTORY_FILE, {}).get(company, {})
    months, today = window_months(company)
    current_key = _month_key(today.year, today.month)
    result, complete = {}, True

    for year, month in months:
        key = _month_key(year, month)
        entry = cache.get(key)
        if entry and key != current_key:
            month_end = dt.date(year + (month == 12), month % 12 + 1, 1)
            frozen = entry.get("final") and (today - month_end).days >= MONTH_FREEZE_DAYS
            fresh = time.time() - entry.get("at", 0) < MONTH_RECHECK_HOURS * 3600
            if frozen or fresh:
                result[key] = entry["events"]
                continue
        if deadline and time.time() > deadline and key != current_key:
            complete = False          # out of budget - next run resumes here
            if entry:
                result[key] = entry["events"]
            continue
        t0 = time.time()
        events = month_events(company, year, month)
        result[key] = events
        if key != current_key:
            month_end = dt.date(year + (month == 12), month % 12 + 1, 1)
            update_history(HISTORY_FILE, company, key, {
                "at": time.time(), "events": events,
                "final": (today - month_end).days >= MONTH_FREEZE_DAYS})
        if progress:
            progress(company, key, time.time() - t0)
    return result, complete


# ---------------------------------------------------------------- linking
def _parse(day):
    return dt.date(int(day[:4]), int(day[5:7]), int(day[8:10]))


def link_callbacks(months):
    """Attach each completed callback to its original install.

    Mutates callback dicts: adds "gap" (days after install completion) and
    "om" (original install's cohort month) when linked. Returns
    {installId: [callbacks]}.
    """
    installs = [i for ev in months.values() for i in ev["installs"]]
    by_id = {i["i"]: i for i in installs}
    by_proj, by_loc = {}, {}
    for i in installs:
        if i.get("proj"):
            by_proj.setdefault(i["proj"], []).append(i)
        by_loc.setdefault(i["loc"], []).append(i)
    for idx in (by_proj, by_loc):
        for lst in idx.values():
            lst.sort(key=lambda x: x["d"])

    def find(cb):
        if cb.get("rf") in by_id:
            return by_id[cb["rf"]]
        # latest install completed on or before the callback was booked
        # (3-day grace: completion is sometimes recorded a bit late)
        ref = cb.get("c") or cb.get("d")
        for key, idx in (("proj", by_proj), ("loc", by_loc)):
            cands = [i for i in idx.get(cb.get(key), ())
                     if (_parse(i["d"]) - _parse(ref)).days <= 3]
            if cands:
                return cands[-1]
        return None

    linked = {}
    for ev in months.values():
        for cb in ev["callbacks"]:
            orig = find(cb)
            if orig is None or not cb.get("d"):
                continue
            cb["gap"] = max(0, (_parse(cb["d"]) - _parse(orig["d"])).days)
            cb["om"] = orig["d"][:7]
            linked.setdefault(orig["i"], []).append(cb)
    return linked


# ---------------------------------------------------------------- aggregate
def _blank_cohort():
    c = {"installs": 0, "visits": 0, "recall": 0, "finish": 0}
    c.update({f"u{w}": 0 for w in WINDOWS})
    return c


def aggregate(months, today):
    """cohorts, monthly trend, callback curve and gap histogram for one
    company (or pre-merged combined months)."""
    linked = link_callbacks(months)
    keys = sorted(months)

    cohorts = {k: _blank_cohort() for k in keys}
    curve_n = 0                       # installs old enough for the full curve
    curve = [0] * 181                 # first-callback count by gap day
    for k in keys:
        for inst in months[k]["installs"]:
            c = cohorts[k]
            c["installs"] += 1
            cbs = linked.get(inst["i"], ())
            c["visits"] += len(cbs)
            c["recall"] += sum(1 for x in cbs if x["cat"] == "recall")
            c["finish"] += sum(1 for x in cbs if x["cat"] == "finish")
            for w in WINDOWS:
                if any(x["gap"] <= w for x in cbs):
                    c[f"u{w}"] += 1
            if (today - _parse(inst["d"])).days > 180:
                curve_n += 1
                first = min((x["gap"] for x in cbs), default=None)
                if first is not None and first <= 180:
                    curve[first] += 1
    for i in range(1, 181):
        curve[i] += curve[i - 1]      # cumulative

    cohort_rows = []
    for k in keys:
        y, m = int(k[:4]), int(k[5:7])
        month_end = dt.date(y + (m == 12), m % 12 + 1, 1) - dt.timedelta(days=1)
        c = cohorts[k]
        c["month"] = k
        c["mature"] = {str(w): (today - month_end).days >= w for w in WINDOWS}
        cohort_rows.append(c)

    # monthly workload trend + gap histogram + recent list
    monthly, hist = [], {"0-7": 0, "8-30": 0, "31-60": 0, "61-90": 0,
                         "91-180": 0, "180+": 0}
    recent, gaps_12mo = [], []
    yr_ago = (today - dt.timedelta(days=365)).isoformat()
    for k in keys:
        ev = months[k]
        rec = sum(1 for x in ev["callbacks"] if x["cat"] == "recall")
        fin = len(ev["callbacks"]) - rec
        # normalize by the installs those callbacks can come from:
        # this month + the 5 before it (~ the 180-day tail); months without
        # a full 6-month pool behind them get no rate rather than a fake one
        pool = sum(cohorts[p]["installs"] for p in keys
                   if p <= k and (int(k[:4]) * 12 + int(k[5:7]))
                   - (int(p[:4]) * 12 + int(p[5:7])) < 6)
        full_pool = keys.index(k) >= 5
        monthly.append({"month": k, "visits": rec + fin, "recall": rec,
                        "finish": fin, "qa": ev["qa"], "drywall": ev["drywall"],
                        "installs": cohorts[k]["installs"],
                        "per100": round((rec + fin) / pool * 100, 1)
                                  if pool and full_pool else None})
        for x in ev["callbacks"]:
            g = x.get("gap")
            if g is None:
                continue
            hist["0-7" if g <= 7 else "8-30" if g <= 30 else "31-60" if g <= 60
                 else "61-90" if g <= 90 else "91-180" if g <= 180
                 else "180+"] += 1
            if x["d"] and x["d"] >= yr_ago:
                gaps_12mo.append(g)
            recent.append({"date": x["d"], "type": x["ty"], "cat": x["cat"],
                           "gap": g, "om": x.get("om")})
    recent.sort(key=lambda r: r["date"] or "", reverse=True)
    gaps_12mo.sort()

    return {
        "cohorts": cohort_rows,
        "monthly": monthly,
        "curve": {"installs": curve_n, "byDay": curve},
        "hist": hist,
        "medianGap": gaps_12mo[len(gaps_12mo) // 2] if gaps_12mo else 0,
        "recent": recent[:RECENT_LIMIT],
    }


def _merge_months(all_months):
    """Merge several companies' month dicts into one 'combined' dict."""
    out = {}
    for months in all_months:
        for k, ev in months.items():
            tgt = out.setdefault(k, {"installs": [], "callbacks": [],
                                     "qa": 0, "drywall": 0})
            tgt["installs"].extend(ev["installs"])
            tgt["callbacks"].extend(ev["callbacks"])
            tgt["qa"] += ev["qa"]
            tgt["drywall"] += ev["drywall"]
    return out


def _kpis(agg, open_cb, today):
    """Headline numbers from the aggregated views."""
    rows = agg["cohorts"]
    cur_key = today.isoformat()[:7]

    def mature_rate(w, last_n=6):
        m = [r for r in rows if r["mature"][str(w)] and r["installs"]][-last_n:]
        inst = sum(r["installs"] for r in m)
        return (sum(r[f"u{w}"] for r in m) / inst * 100) if inst else 0, inst

    r30, _ = mature_rate(30)
    r90, _ = mature_rate(90)
    r180, n180 = mature_rate(180)
    yr = [m for m in agg["monthly"] if m["month"] != cur_key][-12:]
    visits_yr = sum(m["visits"] for m in yr)
    installs_yr = sum(m["installs"] for m in yr)
    mtd = next((m for m in agg["monthly"] if m["month"] == cur_key),
               {"visits": 0, "recall": 0, "finish": 0, "installs": 0})
    rec_share = (sum(m["recall"] for m in yr) / visits_yr * 100) if visits_yr else 0
    return {
        "rate30": round(r30, 1), "rate90": round(r90, 1),
        "rate180": round(r180, 1), "rate180Installs": n180,
        "visitsPer100": round(visits_yr / installs_yr * 100, 1) if installs_yr else 0,
        "visitsYr": visits_yr,
        "mtdVisits": mtd["visits"], "mtdRecall": mtd["recall"],
        "mtdFinish": mtd["finish"], "mtdInstalls": mtd["installs"],
        "openCallbacks": open_cb,
        "medianGap": agg["medianGap"],
        "recallShare": round(rec_share, 1),
    }


# ---------------------------------------------------------------- public API
def compute(time_budget_secs=None, progress=None):
    deadline = time.time() + time_budget_secs if time_budget_secs else None

    def one(company):
        months, ok = compute_company(company, deadline=deadline, progress=progress)
        try:
            open_cb = open_callbacks(company)
        except Exception as e:
            print(f"WARNING: {company} open-callback scan failed ({e})", flush=True)
            open_cb = None
        return months, ok, open_cb

    results = map_companies(one, COMPANIES)
    today = local_today("pacific")
    boards, complete = {}, True
    month_sets, open_total = [], 0
    for company, (months, ok, open_cb) in results.items():
        complete = complete and ok
        month_sets.append(months)
        open_total += open_cb or 0
        agg = aggregate(months, today)
        for r in agg["recent"]:
            r["co"] = COMPANIES[company]["label"]
        boards[company] = dict(agg, kpis=_kpis(agg, open_cb, today))

    combined = aggregate(_merge_months(month_sets), today)
    combined["recent"] = sorted(
        (r for c in COMPANIES for r in boards[c]["recent"]),
        key=lambda r: r["date"] or "", reverse=True)[:RECENT_LIMIT]
    boards["combined"] = dict(combined,
                              kpis=_kpis(combined, open_total, today))

    return {
        "updated": dt.datetime.now().strftime("%a %b %d %Y %H:%M:%S"),
        "complete": complete,
        "today": today.isoformat(),
        "companies": {c: {"label": co["label"], "color": co["color"]}
                      for c, co in COMPANIES.items()},
        "windows": list(WINDOWS),
        "boards": boards,
    }


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) > 2:
        company, ym = sys.argv[1], sys.argv[2]
        y, m = map(int, ym.split("-"))
        ev = month_events(company, y, m)
        print(f"{company} {ym}: {len(ev['installs'])} installs, "
              f"{len(ev['callbacks'])} callbacks "
              f"({sum(1 for c in ev['callbacks'] if c['cat']=='recall')} recall), "
              f"qa {ev['qa']}, drywall {ev['drywall']}")
    else:
        t0 = time.time()
        data = compute(progress=lambda co, k, s: print(f"  {co} {k} {s:.1f}s",
                                                       flush=True))
        for c, b in data["boards"].items():
            k = b["kpis"]
            print(f"{c:9s} 30d {k['rate30']:4.1f}%  90d {k['rate90']:4.1f}%  "
                  f"180d {k['rate180']:4.1f}%  visits/100 {k['visitsPer100']:5.1f}  "
                  f"open {k['openCallbacks']}")
        print(f"-- computed in {time.time() - t0:.0f}s "
              f"(complete={data['complete']})")
