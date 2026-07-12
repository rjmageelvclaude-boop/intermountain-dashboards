#!/usr/bin/env python3
"""
Live ServiceTitan engine for the 4 Day Call board.

Five boards, each a (company, trade) pair scoped to specific business units:

    sierra-hvac        SIE   HVAC - Service, HVAC - Maintenance
    sierra-plumbing    SIE   Plumbing - Service, Plumbing - Maintenance, Plumbing - Drains
    ultimate-hvac      ULT   HVAC - Service, HVAC - Maintenance
    ultimate-plumbing  ULT   Plumbing - Service, Plumbing - Maintenance
    russett-hvac       RUS   HVAC - Service, HVAC - Maintenance

Per board, for each of the next 4 calendar days (starting today, company-local):
  - opps / nonOpps = calls on board (jobs with an appointment that day, not
    canceled, in the board's BUs) split by ServiceTitan's PER-JOB Opportunity
    flag. The flag is not a field on the job record, but it is fully determined
    by fields that are:

        opportunity = (not job.noCharge) or (job.total >= jobType.soldThreshold)

    i.e. a job is an opportunity unless it's marked No Charge, and a No Charge
    job flips back to an opportunity once its invoice reaches the job type's
    sold threshold (default $100 - covers trip-fee-only jobs staying non-opp).
    Reverse-engineered against the Reporting API's per-job Opportunity column
    ("HVAC Total Call Count_Claude_RJ"): 358/358 exact across HVAC + plumbing,
    2026-07-09 and 2026-07-10; future days match as well (no invoice yet, so
    it reduces to !noCharge).
  - ropps = calls on board carrying the board's replacement-opp tag, excluding
    jobs that also carry the Management Removed ROPP tag. HVAC boards use the
    ROPP tag; plumbing boards use TROP (Sierra) / T-ROPP (Ultimate).

Techs available / calls needed / replacement opps needed are manual inputs
entered on the dashboard itself (shared through the Apps Script budget store);
they are not computed here.

The dashboard also carries an "Upcoming Jobs" view per company: calls on the
board by day and business unit for the next 60 days (appointments joined to
jobs; canceled appointments/jobs excluded), with yesterday's snapshot kept on
disk so the page can show day-over-day changes.

CLI smoke test:
    py build/call_board_live.py                # all boards
    py build/call_board_live.py sierra-hvac    # one board (still fetches its tenant)
"""
import datetime as dt
import json
import os
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from command_center_live import (fetch_all, local_day_window_utc, local_today,
                                 _load_json, _save_json, _utc_offset_hours)

CONFIG_CACHE = os.path.join(ROOT, "data", "call-board-st-config.json")
UPCOMING_HISTORY = os.path.join(ROOT, "data", "call-board-upcoming-history.json")
CONFIG_TTL_HOURS = 24 * 7   # job-type soldThreshold cache
BOARD_DAYS = 4
DEFAULT_SOLD_THRESHOLD = 100.0
UPCOMING_DAYS = 60
UPCOMING_HISTORY_KEEP_DAYS = 35

COMPANIES = {
    "sierra": {
        "tenant": "SIE", "tz": "pacific",
        "name": "Sierra Air Conditioning & Plumbing", "short": "Sierra",
        "weather": {"lat": 36.17, "lon": -115.14, "tz": "America/Los_Angeles"},  # Las Vegas
        # Upcoming Jobs view: business-unit columns, in display order
        "cols": [
            {"key": "hvac_svc", "label": "Service", "group": "HVAC", "bu": 333},
            {"key": "hvac_mnt", "label": "Maint.", "group": "HVAC", "bu": 342817560},
            {"key": "plumb_drn", "label": "Drains", "group": "Plumbing", "bu": 595105985},
            {"key": "plumb_mnt", "label": "Maint.", "group": "Plumbing", "bu": 354},
            {"key": "plumb_svc", "label": "Service", "group": "Plumbing", "bu": 353},
        ],
    },
    "ultimate": {
        "tenant": "ULT", "tz": "mountain",
        "name": "Ultimate Heating Air & Plumbing", "short": "Ultimate",
        "weather": {"lat": 43.615, "lon": -116.202, "tz": "America/Boise"},      # Boise
        "cols": [
            {"key": "hvac_svc", "label": "Service", "group": "HVAC", "bu": 2691},
            {"key": "hvac_mnt", "label": "Maint.", "group": "HVAC", "bu": 2692},
            {"key": "plumb_mnt", "label": "Maint.", "group": "Plumbing", "bu": 128196},
            {"key": "plumb_svc", "label": "Service", "group": "Plumbing", "bu": 8450},
        ],
    },
    "russett": {
        "tenant": "RUS", "tz": "arizona",
        "name": "Russett Southwest", "short": "Russett",
        "weather": {"lat": 32.222, "lon": -110.975, "tz": "America/Phoenix"},    # Tucson
        "cols": [
            {"key": "hvac_svc", "label": "Service", "group": "HVAC", "bu": 221},
            {"key": "hvac_mnt", "label": "Maint.", "group": "HVAC", "bu": 53208412},
        ],
    },
}

BOARDS = {
    "sierra-hvac": {
        "company": "sierra", "trade": "HVAC",
        "title": "HVAC - Service & Maintenance",
        "bus": [333, 342817560],
        "ropp_tags": [962027],             # "ROPP"
        "ropp_removed_tags": [545867780],  # "Management Removed ROPP"
    },
    "sierra-plumbing": {
        "company": "sierra", "trade": "Plumbing",
        "title": "Plumbing - Service, Maintenance & Drains",
        "bus": [353, 354, 595105985],
        "ropp_tags": [396774589],          # "TROP" (Sierra's plumbing replacement-opp tag)
        "ropp_removed_tags": [545867780],
    },
    "ultimate-hvac": {
        "company": "ultimate", "trade": "HVAC",
        "title": "HVAC - Service & Maintenance",
        "bus": [2691, 2692],
        "ropp_tags": [52206586],           # "ROPP" (not "Possible ROPP")
        "ropp_removed_tags": [],           # tenant has no Management Removed ROPP tag
    },
    "ultimate-plumbing": {
        "company": "ultimate", "trade": "Plumbing",
        "title": "Plumbing - Service & Maintenance",
        "bus": [8450, 128196],
        "ropp_tags": [79756058],           # "T-ROPP" (not "Possible TROPP")
        "ropp_removed_tags": [],
    },
    "russett-hvac": {
        "company": "russett", "trade": "HVAC",
        "title": "HVAC - Service & Maintenance",
        "bus": [221, 53208412],
        "ropp_tags": [63640008],           # "ROPP"
        "ropp_removed_tags": [],
    },
}


def board_days(tz, n=BOARD_DAYS):
    """The next n calendar days starting today (company-local), weekends included."""
    today = local_today(tz)
    return [today + dt.timedelta(days=i) for i in range(n)]


def job_type_thresholds(tenant):
    """{job_type_id: soldThreshold} cached on disk (job types rarely change)."""
    cache = _load_json(CONFIG_CACHE, {})
    entry = cache.get(f"{tenant}:thresholds")
    if entry and time.time() - entry.get("at", 0) < CONFIG_TTL_HOURS * 3600:
        return {int(k): v for k, v in entry["thresholds"].items()}
    thresholds = {str(j["id"]): (j.get("soldThreshold") if j.get("soldThreshold") is not None
                                 else DEFAULT_SOLD_THRESHOLD)
                  for j in fetch_all(tenant, "/jpm/v2/tenant/{tenant}/job-types",
                                     {"active": "Any"}, page_size=100)}
    cache[f"{tenant}:thresholds"] = {"at": time.time(), "thresholds": thresholds}
    _save_json(CONFIG_CACHE, cache)
    return {int(k): v for k, v in thresholds.items()}


def is_opportunity(job, thresholds):
    """ServiceTitan's per-job Opportunity flag, reconstructed from job fields."""
    if not job.get("noCharge"):
        return True
    thresh = thresholds.get(job.get("jobTypeId"), DEFAULT_SOLD_THRESHOLD)
    return float(job.get("total") or 0) >= thresh


# ------------------------------------------------------------------ weather
def _weather(company):
    """Min/max forecast from Open-Meteo (free, no key). None on failure."""
    w = COMPANIES[company]["weather"]
    url = ("https://api.open-meteo.com/v1/forecast"
           f"?latitude={w['lat']}&longitude={w['lon']}"
           "&daily=temperature_2m_max,temperature_2m_min,weather_code"
           "&temperature_unit=fahrenheit&forecast_days=10"
           f"&timezone={urllib.parse.quote(w['tz'])}")
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            d = json.load(resp)["daily"]
        return {date: {"min": round(mn), "max": round(mx), "code": code}
                for date, mn, mx, code in zip(d["time"], d["temperature_2m_min"],
                                              d["temperature_2m_max"], d["weather_code"])}
    except Exception as e:
        print(f"weather fetch failed for {company}: {e}", file=sys.stderr)
        return None


# ------------------------------------------------------------ upcoming jobs
def _parse_st_ts(ts):
    """ServiceTitan UTC timestamp, with or without fractional seconds."""
    return dt.datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")


def _company_pull(company, n_days=UPCOMING_DAYS):
    """One bulk pull per company covering the next n_days: jobs joined to their
    appointments, bucketed by local day. Returns (day_jobs, by_day) where
    day_jobs = {iso: [job, ...]} (deduped per day, board BUs only, canceled
    jobs/appointments excluded) and by_day = {iso: {col_key: count}}."""
    co = COMPANIES[company]
    tenant = co["tenant"]
    tz = co["tz"]
    today = local_today(tz)
    last_day = today + dt.timedelta(days=n_days - 1)
    start, _ = local_day_window_utc(tz, today)
    _, end = local_day_window_utc(tz, last_day)
    bu_key = {c["bu"]: c["key"] for c in co["cols"]}

    jobs = fetch_all(tenant, "/jpm/v2/tenant/{tenant}/jobs",
                     {"appointmentStartsOnOrAfter": start, "appointmentStartsBefore": end},
                     page_size=500, max_pages=200)
    job_by_id = {j["id"]: j for j in jobs
                 if j.get("jobStatus") != "Canceled" and j.get("businessUnitId") in bu_key}

    appts = fetch_all(tenant, "/jpm/v2/tenant/{tenant}/appointments",
                      {"startsOnOrAfter": start, "startsBefore": end},
                      page_size=500, max_pages=200)

    by_day = {(today + dt.timedelta(days=i)).isoformat(): {c["key"]: 0 for c in co["cols"]}
              for i in range(n_days)}
    day_jobs = {iso: [] for iso in by_day}
    seen = set()
    for a in appts:
        if a.get("status") == "Canceled":
            continue
        job = job_by_id.get(a.get("jobId"))
        if not job:
            continue
        try:
            ts = _parse_st_ts(a["start"])
        except (KeyError, ValueError, TypeError):
            continue
        local_date = (ts + dt.timedelta(hours=_utc_offset_hours(tz, ts.date()))).date()
        iso = local_date.isoformat()
        if iso not in by_day or (iso, job["id"]) in seen:
            continue
        seen.add((iso, job["id"]))
        by_day[iso][bu_key[job["businessUnitId"]]] += 1
        day_jobs[iso].append(job)
    return day_jobs, by_day


def upcoming_output(company, by_day):
    """The Upcoming Jobs feed + day-over-day change tracking: one snapshot per
    local day kept on disk; the page compares against the most recent snapshot
    before today."""
    co = COMPANIES[company]
    today = local_today(co["tz"]).isoformat()
    hist = _load_json(UPCOMING_HISTORY, {})
    mine = hist.setdefault(company, {})
    mine[today] = by_day
    cutoff = (local_today(co["tz"]) - dt.timedelta(days=UPCOMING_HISTORY_KEEP_DAYS)).isoformat()
    hist[company] = {k: v for k, v in mine.items() if k >= cutoff}
    _save_json(UPCOMING_HISTORY, hist)
    prev_stamp = max((k for k in hist[company] if k < today), default=None)

    return {
        "cols": [{"key": c["key"], "label": c["label"], "group": c["group"]} for c in co["cols"]],
        "days": [{"date": iso, "dow": dt.date.fromisoformat(iso).strftime("%A"),
                  "counts": counts} for iso, counts in sorted(by_day.items())],
        "prevDate": prev_stamp,
        "prevByDay": hist[company].get(prev_stamp) if prev_stamp else None,
    }


# --------------------------------------------------------------- metric core
def compute(only_board=None):
    wanted = {k: b for k, b in BOARDS.items() if only_board in (None, k)}
    if not wanted:
        raise ValueError(f"Unknown board '{only_board}'. Known: {', '.join(BOARDS)}")

    out = {"generatedAt": dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
           "boards": {}, "weather": {}, "upcoming": {}}

    # One bulk 60-day pull per company feeds both the 4-day boards and the
    # Upcoming Jobs view.
    day_jobs = {}
    companies = {b["company"] for b in wanted.values()}
    for company in companies:
        out["weather"][company] = _weather(company)
        day_jobs[company], by_day = _company_pull(company)
        out["upcoming"][company] = upcoming_output(company, by_day)

    for key, b in wanted.items():
        company = b["company"]
        co = COMPANIES[company]
        thresholds = job_type_thresholds(co["tenant"])
        bus = set(b["bus"])
        ropp = set(b["ropp_tags"])
        removed = set(b["ropp_removed_tags"])
        days = []
        for day in board_days(co["tz"]):
            jobs = [j for j in day_jobs[company].get(day.isoformat(), [])
                    if j.get("businessUnitId") in bus]
            opps = non_opps = ropps = 0
            for j in jobs:
                if is_opportunity(j, thresholds):
                    opps += 1
                else:
                    non_opps += 1
                tags = set(j.get("tagTypeIds") or [])
                if (ropp & tags) and not (removed & tags):
                    ropps += 1
            days.append({"date": day.isoformat(), "dow": day.strftime("%A"),
                         "opps": opps, "nonOpps": non_opps, "ropps": ropps})
        out["boards"][key] = {
            "company": company, "companyName": co["name"], "companyShort": co["short"],
            "trade": b["trade"], "title": b["title"], "days": days,
        }
    return out


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    only = sys.argv[1] if len(sys.argv) > 1 else None
    t0 = time.time()
    print(json.dumps(compute(only), indent=2))
    print(f"-- computed in {time.time() - t0:.1f}s", file=sys.stderr)
