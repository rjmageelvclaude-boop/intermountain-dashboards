#!/usr/bin/env python3
"""
Live ServiceTitan engine for the 4 Day Call board.

Five boards, each a (company, trade) pair scoped to specific business units:

    sierra-hvac        SIE   HVAC - Service, HVAC - Maintenance
    sierra-plumbing    SIE   Plumbing - Service, Plumbing - Maintenance, Plumbing - Drains
    ultimate-hvac      ULT   HVAC - Service, HVAC - Maintenance
    ultimate-plumbing  ULT   Plumbing - Service, Plumbing - Maintenance
    russett-hvac       RUS   HVAC - Service, HVAC - Maintenance

Per board, for each of the next 4 weekdays (starting today, company-local):
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
from command_center_live import fetch_all, local_day_window_utc, local_today, _load_json, _save_json

CONFIG_CACHE = os.path.join(ROOT, "data", "call-board-st-config.json")
CONFIG_TTL_HOURS = 24 * 7   # job-type soldThreshold cache
BOARD_DAYS = 4
DEFAULT_SOLD_THRESHOLD = 100.0

COMPANIES = {
    "sierra": {
        "tenant": "SIE", "tz": "pacific",
        "name": "Sierra Air Conditioning & Plumbing", "short": "Sierra",
        "weather": {"lat": 36.17, "lon": -115.14, "tz": "America/Los_Angeles"},  # Las Vegas
    },
    "ultimate": {
        "tenant": "ULT", "tz": "mountain",
        "name": "Ultimate Heating Air & Plumbing", "short": "Ultimate",
        "weather": {"lat": 43.615, "lon": -116.202, "tz": "America/Boise"},      # Boise
    },
    "russett": {
        "tenant": "RUS", "tz": "arizona",
        "name": "Russett Southwest", "short": "Russett",
        "weather": {"lat": 32.222, "lon": -110.975, "tz": "America/Phoenix"},    # Tucson
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
    """The next n weekdays starting today (company-local); weekends skipped."""
    days, d = [], local_today(tz)
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d += dt.timedelta(days=1)
    return days


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


# --------------------------------------------------------------- metric core
def _board_jobs(tenant, tz, day):
    """Non-canceled jobs with an appointment starting on the given local day."""
    start, end = local_day_window_utc(tz, day)
    jobs = fetch_all(tenant, "/jpm/v2/tenant/{tenant}/jobs",
                     {"appointmentStartsOnOrAfter": start, "appointmentStartsBefore": end})
    return [j for j in jobs if j.get("jobStatus") != "Canceled"]


def compute(only_board=None):
    wanted = {k: b for k, b in BOARDS.items() if only_board in (None, k)}
    if not wanted:
        raise ValueError(f"Unknown board '{only_board}'. Known: {', '.join(BOARDS)}")

    out = {"generatedAt": dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
           "boards": {}, "weather": {}}

    companies = {b["company"] for b in wanted.values()}
    for company in companies:
        out["weather"][company] = _weather(company)

    # One jobs fetch per (tenant, day), shared by every board on that tenant.
    day_jobs = {}
    for company in companies:
        co = COMPANIES[company]
        for day in board_days(co["tz"]):
            day_jobs[(company, day)] = _board_jobs(co["tenant"], co["tz"], day)

    for key, b in wanted.items():
        company = b["company"]
        co = COMPANIES[company]
        thresholds = job_type_thresholds(co["tenant"])
        bus = set(b["bus"])
        ropp = set(b["ropp_tags"])
        removed = set(b["ropp_removed_tags"])
        days = []
        for day in board_days(co["tz"]):
            jobs = [j for j in day_jobs[(company, day)] if j.get("businessUnitId") in bus]
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
