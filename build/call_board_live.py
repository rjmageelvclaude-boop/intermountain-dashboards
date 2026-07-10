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
  - opps      = calls on board that are opportunities: jobs with an appointment
                that day, not canceled, in the board's BUs, whose job-type class
                is an opportunity class (Demand / Marketed Tune Up / System Check)
  - nonOpps   = same but job-type class is NOT an opportunity class
                (Non-Opportunity, Callback/Warranty, Return to Complete Repair, ...)
  - ropps     = calls on board carrying the ROPP tag, excluding jobs that also
                carry the Management Removed ROPP tag

Techs available / calls needed / replacement opps needed are manual inputs
entered on the dashboard itself (shared through the Apps Script budget store);
they are not computed here.

CLI smoke test:
    py build/call_board_live.py                # all boards
    py build/call_board_live.py sierra-hvac    # one board
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
CONFIG_TTL_HOURS = 24 * 7
BOARD_DAYS = 4

# Job-type classes ServiceTitan treats as opportunities on the dispatch board.
OPPORTUNITY_CLASSES = {"Demand", "Marketed Tune Up", "System Check"}

COMPANIES = {
    "sierra": {
        "tenant": "SIE", "tz": "pacific",
        "name": "Sierra Air Conditioning & Plumbing", "short": "Sierra",
        "ropp_tags": [962027],            # "ROPP"
        "ropp_removed_tags": [545867780], # "Management Removed ROPP"
        "weather": {"lat": 36.17, "lon": -115.14, "tz": "America/Los_Angeles"},  # Las Vegas
    },
    "ultimate": {
        "tenant": "ULT", "tz": "mountain",
        "name": "Ultimate Heating Air & Plumbing", "short": "Ultimate",
        "ropp_tags": [52206586],          # "ROPP" (not "Possible ROPP")
        "ropp_removed_tags": [],          # tenant has no Management Removed ROPP tag
        "weather": {"lat": 43.615, "lon": -116.202, "tz": "America/Boise"},      # Boise
    },
    "russett": {
        "tenant": "RUS", "tz": "arizona",
        "name": "Russett Southwest", "short": "Russett",
        "ropp_tags": [63640008],          # "ROPP"
        "ropp_removed_tags": [],          # tenant has no Management Removed ROPP tag
        "weather": {"lat": 32.222, "lon": -110.975, "tz": "America/Phoenix"},    # Tucson
    },
}

BOARDS = {
    "sierra-hvac": {
        "company": "sierra", "trade": "HVAC",
        "title": "HVAC - Service & Maintenance",
        "bus": [333, 342817560],
    },
    "sierra-plumbing": {
        "company": "sierra", "trade": "Plumbing",
        "title": "Plumbing - Service, Maintenance & Drains",
        "bus": [353, 354, 595105985],
    },
    "ultimate-hvac": {
        "company": "ultimate", "trade": "HVAC",
        "title": "HVAC - Service & Maintenance",
        "bus": [2691, 2692],
    },
    "ultimate-plumbing": {
        "company": "ultimate", "trade": "Plumbing",
        "title": "Plumbing - Service & Maintenance",
        "bus": [8450, 128196],
    },
    "russett-hvac": {
        "company": "russett", "trade": "HVAC",
        "title": "HVAC - Service & Maintenance",
        "bus": [221, 53208412],
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


def job_type_classes(tenant):
    """{job_type_id: class} cached on disk (job types rarely change)."""
    cache = _load_json(CONFIG_CACHE, {})
    entry = cache.get(f"{tenant}:classes")
    if entry and time.time() - entry.get("at", 0) < CONFIG_TTL_HOURS * 3600:
        return {int(k): v for k, v in entry["classes"].items()}
    classes = {str(j["id"]): (j.get("class") or "") for j in
               fetch_all(tenant, "/jpm/v2/tenant/{tenant}/job-types", {"active": "Any"}, page_size=100)}
    cache[f"{tenant}:classes"] = {"at": time.time(), "classes": classes}
    _save_json(CONFIG_CACHE, cache)
    return {int(k): v for k, v in classes.items()}


def _board_jobs(tenant, tz, day):
    """Non-canceled jobs with an appointment starting on the given local day."""
    start, end = local_day_window_utc(tz, day)
    jobs = fetch_all(tenant, "/jpm/v2/tenant/{tenant}/jobs",
                     {"appointmentStartsOnOrAfter": start, "appointmentStartsBefore": end})
    return [j for j in jobs if j.get("jobStatus") != "Canceled"]


def _weather(company):
    """4-day min/max forecast from Open-Meteo (free, no key). None on failure."""
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


def compute(only_board=None):
    """All boards -> {generatedAt, boards: {key: {..., days: [...]}}, weather: {company: {date: ...}}}."""
    wanted = {k: b for k, b in BOARDS.items() if only_board in (None, k)}
    if not wanted:
        raise ValueError(f"Unknown board '{only_board}'. Known: {', '.join(BOARDS)}")

    # One jobs fetch per (tenant, day), shared by every board on that tenant.
    day_jobs = {}
    for key, b in wanted.items():
        co = COMPANIES[b["company"]]
        for day in board_days(co["tz"]):
            cache_key = (co["tenant"], day)
            if cache_key not in day_jobs:
                day_jobs[cache_key] = _board_jobs(co["tenant"], co["tz"], day)

    out = {"generatedAt": dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
           "boards": {}, "weather": {}}

    for company in {b["company"] for b in wanted.values()}:
        out["weather"][company] = _weather(company)

    for key, b in wanted.items():
        co = COMPANIES[b["company"]]
        classes = job_type_classes(co["tenant"])
        bus = set(b["bus"])
        ropp = set(co["ropp_tags"])
        removed = set(co["ropp_removed_tags"])
        days = []
        for day in board_days(co["tz"]):
            jobs = [j for j in day_jobs[(co["tenant"], day)] if j.get("businessUnitId") in bus]
            opps = non_opps = ropps = 0
            for j in jobs:
                tags = set(j.get("tagTypeIds") or [])
                if classes.get(j.get("jobTypeId")) in OPPORTUNITY_CLASSES:
                    opps += 1
                else:
                    non_opps += 1
                if (ropp & tags) and not (removed & tags):
                    ropps += 1
            days.append({"date": day.isoformat(), "dow": day.strftime("%A"),
                         "opps": opps, "nonOpps": non_opps, "ropps": ropps})
        out["boards"][key] = {
            "company": b["company"], "companyName": co["name"], "companyShort": co["short"],
            "trade": b["trade"], "title": b["title"], "days": days,
        }
    return out


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    only = sys.argv[1] if len(sys.argv) > 1 else None
    t0 = time.time()
    print(json.dumps(compute(only), indent=2))
    print(f"-- computed in {time.time() - t0:.1f}s", file=sys.stderr)
