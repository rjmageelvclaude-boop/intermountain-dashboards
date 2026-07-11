#!/usr/bin/env python3
"""
Live engine for the GOAT Group trip tracker at /goat-board/.

The GOAT Group is the 2026 incentive trip: hit your calendar-year goal and
you (plus a guest) go. One board, every department, everyone ranked by
percent-to-goal:

    Comfort Advisors      $4,000,000  sales             ca-board data.json
    HVAC Installers       $1,750,000  installed revenue install-board data.json
    Technicians             $350,000  service revenue   tech-board data.json
    SILO Techs            $3,500,000  TGL revenue       computed here
    Plumbers               $2,000,000 sales             plumb-board data.json
    Plumbing Installers   $1,500,000  installed revenue computed here
    Managers              20% of direct reports qualify pending (no mapping)

Four departments are read straight from the sibling boards' YTD views (this
refresh runs after theirs in the workflow, so the numbers are the same ones
on those boards). SILO techs and plumbing installers aren't on any board, so
their teams are computed here with the tech-board month engine:

    SILO techs           SIE team "1Silo", ULT team "1 - SILO Techs";
                         amount = TGL sales (sold estimate subtotals on jobs
                         the tech generated, jobGeneratedLeadSource)
    plumbing installers  SIE teams "Plumber - Installers" and
                         "Plumbing Senior Install tech"; amount = completed
                         job totals x payroll split % (install-board revenue)

Closed months for the live groups are cached in data/goat-board-history.json
with the same freeze/recheck rules as the tech board.

CLI smoke test:
    py build/goat_board_live.py            # full payload summary
    py build/goat_board_live.py rosters    # live-group rosters only
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

import tech_board_live as tb
from command_center_live import fetch_all, local_today, _load_json, _save_json

HISTORY_FILE = os.path.join(ROOT, "data", "goat-board-history.json")
SITE_DIR = os.path.join(ROOT, "site")

PALETTE = {
    "sierra": {"label": "Sierra", "code": "SIE", "color": "#1663c7"},
    "ultimate": {"label": "Ultimate", "code": "ULT", "color": "#c7161d"},
    "russett": {"label": "Russett", "code": "RSW", "color": "#0e7a3d"},
}

# Teams no other board tracks, computed live (normalized team name -> group).
LIVE_TEAMS = {
    "sierra": {
        "silo": ("1silo",),
        "plumbInstall": ("plumber - installers", "plumbing senior install tech"),
    },
    "ultimate": {
        "silo": ("1 - silo techs",),
    },
}


# ---------------------------------------------------------------- live groups
def live_roster(company):
    """Active technicians on the live-group teams: {techId: {..., group}}."""
    co = tb.COMPANIES[company]
    groups = LIVE_TEAMS.get(company) or {}
    techs = fetch_all(co["tenant"], "/settings/v2/tenant/{tenant}/technicians",
                      {"active": "True"}, page_size=200)
    roster = {}
    for t in techs:
        team = re.sub(r"\s+", " ", (t.get("team") or "").strip().lower())
        for gname, teams in groups.items():
            if team in teams:
                roster[t["id"]] = {
                    "id": t["id"],
                    "userId": t.get("userId"),
                    "name": tb.clean_name(t.get("name")),
                    "team": (t.get("team") or "").strip(),
                    "group": gname,
                }
    return roster


def compute_live_company(company, deadline=None, progress=None):
    """Tech-board month engine over the live-group roster, own month cache.
    Returns (months_dict, roster, complete)."""
    def _int_keys(techs):
        return {int(k): v for k, v in techs.items()}

    cache = _load_json(HISTORY_FILE, {})
    co_cache = cache.setdefault(company, {})
    months, today = tb.months_of_year(company)
    roster = live_roster(company)
    thresholds = tb.sold_thresholds(tb.COMPANIES[company]["tenant"])
    current_key = tb._month_key(today.year, today.month)
    complete = True

    result = {}
    for year, month in months:
        key = tb._month_key(year, month)
        entry = co_cache.get(key)
        if entry and key != current_key:
            month_end = dt.date(year + (month == 12), month % 12 + 1, 1)
            frozen = (today - month_end).days >= tb.MONTH_FREEZE_DAYS and entry.get("final")
            fresh = time.time() - entry.get("at", 0) < tb.MONTH_RECHECK_HOURS * 3600
            if frozen or fresh:
                result[key] = _int_keys(entry["techs"])
                continue
        if deadline and time.time() > deadline and key != current_key:
            complete = False   # out of time - keep whatever cache we have
            if entry:
                result[key] = _int_keys(entry["techs"])
            continue
        t0 = time.time()
        techs = tb.compute_month(company, year, month, roster=roster, thresholds=thresholds)
        result[key] = techs
        if key != current_key:
            month_end = dt.date(year + (month == 12), month % 12 + 1, 1)
            co_cache[key] = {"at": time.time(), "techs": techs,
                             "final": (today - month_end).days >= tb.MONTH_FREEZE_DAYS}
            cache[company] = co_cache
            _save_json(HISTORY_FILE, cache)
        if progress:
            progress(f"goat/{company}", key, time.time() - t0)
    return result, roster, complete


def live_rows(deadline=None, progress=None):
    """{'silo': rows, 'plumbInstall': rows} ranked later; plus complete flag."""
    rows = {"silo": [], "plumbInstall": []}
    complete = True
    for company in LIVE_TEAMS:
        per_month, roster, ok = compute_live_company(company, deadline, progress)
        complete = complete and ok
        pal = PALETTE[company]
        for tid, info in roster.items():
            tot = tb._sum_counters(
                [m.get(tid) or tb._new_counters() for m in per_month.values()])
            amount = tot["tglSales"] if info["group"] == "silo" else tot["revenue"]
            rows[info["group"]].append({
                "id": tid,
                "name": info["name"],
                "team": info["team"],
                "company": company,
                "companyLabel": pal["label"],
                "companyCode": pal["code"],
                "color": pal["color"],
                "amount": round(amount, 2),
            })
    return rows, complete


# ---------------------------------------------------------------- board feeds
def _read_board(name):
    try:
        with open(os.path.join(SITE_DIR, name, "data.json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def rows_from_ca():
    d = _read_board("ca-board")
    if not d:
        return None
    rows = []
    for key, co in (d.get("companies") or {}).items():
        pal = PALETTE.get(key) or {"label": co.get("label"), "code": "?",
                                   "color": co.get("color")}
        for a in co["periods"]["ytd"]["advisors"]:
            rows.append({
                "id": a.get("id"),
                "name": a["name"],
                "team": a.get("dept") or "",
                "company": key,
                "companyLabel": pal["label"],
                "companyCode": pal["code"],
                "color": pal["color"],
                "amount": round(float(a.get("sales") or 0), 2),
            })
    return rows


def rows_from_board(name, field):
    d = _read_board(name)
    if not d:
        return None
    rows = []
    for key, lst in (d.get("boards") or {}).get("ytd", {}).items():
        if key == "combined":
            continue
        pal = PALETTE.get(key) or {"label": key, "code": "?", "color": "#555"}
        for r in lst:
            rows.append({
                "id": r.get("id"),
                "name": r["name"],
                "team": r.get("team") or "",
                "company": key,
                "companyLabel": pal["label"],
                "companyCode": pal["code"],
                "color": pal["color"],
                "amount": round(float(r.get(field) or 0), 2),
            })
    return rows


# ---------------------------------------------------------------- public API
def compute(time_budget_secs=None, progress=None):
    """Full data.json payload for the GOAT board."""
    deadline = time.time() + time_budget_secs if time_budget_secs else None
    live, complete = live_rows(deadline, progress)
    today = local_today("pacific")

    departments = []

    def add(key, label, goal, metric, rows, source, note=None):
        missing = rows is None
        rows = sorted(rows or [], key=lambda r: -r["amount"])
        departments.append({
            "key": key, "label": label, "goal": goal, "metric": metric,
            "source": source, "note": note,
            "missing": missing, "people": rows,
        })

    add("ca", "Comfort Advisors", 4_000_000, "YTD Sales",
        rows_from_ca(), "ca-board")
    add("hvacInstall", "HVAC Installers", 1_750_000, "YTD Installed Revenue",
        rows_from_board("install-board", "revenue"), "install-board")
    add("tech", "Technicians", 350_000, "YTD Service Revenue",
        rows_from_board("tech-board", "revenue"), "tech-board",
        note="Repairs + maintenance + IAQ service revenue; TGL install revenue lands on the install crews, not here")
    add("silo", "SILO Techs", 3_500_000, "YTD TGL Revenue",
        live["silo"], "live",
        note="Sold estimate revenue on jobs the tech generated (TGL)")
    add("plumb", "Plumbers & Electricians", 2_000_000, "YTD Sales",
        rows_from_board("plumb-board", "sales"), "plumb-board",
        note="No electrician teams in ServiceTitan yet - plumbers only")
    add("plumbInstall", "Plumbing Installers", 1_500_000, "YTD Installed Revenue",
        live["plumbInstall"], "live",
        note="Completed job totals x payroll split, Sierra plumbing install teams")
    departments.append({
        "key": "managers", "label": "Managers", "goal": None,
        "metric": "20% of direct reports qualified", "source": "pending",
        "note": "Waiting on the manager - direct report mapping (ServiceTitan teams don't capture it)",
        "missing": False, "pending": True, "people": [],
    })

    return {
        "updated": dt.datetime.now().strftime("%a %b %d %Y %H:%M:%S"),
        "generatedAt": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "complete": complete,
        "year": today.year,
        "departments": departments,
    }


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) > 1 and sys.argv[1] == "rosters":
        for company in LIVE_TEAMS:
            print(f"== {company}")
            for t in sorted(live_roster(company).values(), key=lambda x: (x["group"], x["name"])):
                print(f"  {t['group']:13s} {t['name']:30s} {t['team']}")
    else:
        data = compute(progress=lambda co, key, secs: print(f"  {co} {key} in {secs:.1f}s", flush=True))
        for d in data["departments"]:
            goal = f"${d['goal']:,.0f}" if d["goal"] else d["metric"]
            print(f"== {d['label']} ({goal})  people={len(d['people'])} missing={d.get('missing')}")
            for r in d["people"][:5]:
                pct = r["amount"] / d["goal"] * 100 if d["goal"] else 0
                print(f"   {r['name']:28s} {r['companyCode']} ${r['amount']:>12,.0f}  {pct:5.1f}%")
