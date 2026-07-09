#!/usr/bin/env python3
"""
Live ServiceTitan metric engine for the InterMountain Command Center.

Replaces the Gmail/Apps Script pipeline: every number on the dashboard is
computed straight from the ServiceTitan API.

    from command_center_live import compute_current, compute_history
    current = compute_current()          # {"sierra": {...}, "ultimate": {...}, "russett": {...}}
    history = compute_history()          # {"sierra": [{date, ...metrics}, ...], ...}

Metric definitions (validated against the old report-based feed):
  - "on board"        = jobs with an appointment starting that local day, not canceled,
                        in the service-side business units (maintenance + demand)
  - "ran"             = those jobs completed that day
  - ROPP              = HVAC service job carrying the company's ROPP tag
  - TGL set           = job created that day whose job type is a TGL type ("... TGL")
  - daily revenue     = sum of invoice subtotals with that invoice date
  - daily sales       = sum of estimates sold that day (sum of line items)
  - leads / booking   = inbound calls: leads are Booked+Unbooked, rate = Booked/leads
  - memberships sold  = membership line items on that day's invoices, split by BU

CLI smoke test:
    py build/command_center_live.py sierra            # today's numbers
    py build/command_center_live.py sierra 2026-07-08 # specific day
"""
import datetime as dt
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from servicetitan_client import st_get

DATA_DIR = os.path.join(ROOT, "data")
CONFIG_CACHE = os.path.join(DATA_DIR, "command-center-st-config.json")
HISTORY_FILE = os.path.join(DATA_DIR, "command-center-history.json")
HISTORY_WEEKDAYS = 20          # sparkline depth (business days)
CONFIG_TTL_HOURS = 24 * 7      # job-type list cache

COMPANIES = {
    "sierra": {
        "tenant": "SIE",
        "tz": "pacific",
        "bu": {
            333: "hvac_demand", 342817560: "hvac_maint",
            370: "hvac_sales", 340802904: "hvac_sales",
            337: "hvac_install",
            353: "plumb_demand", 595105985: "plumb_demand",
            354: "plumb_maint", 408662213: "plumb_install",
        },
        "costco_bu": [340802904],
        "ropp_tags": [962027],
        "plumb": True, "costco": True,
    },
    "ultimate": {
        "tenant": "ULT",
        "tz": "mountain",
        "bu": {
            2691: "hvac_demand", 2692: "hvac_maint", 2693: "hvac_sales",
            12932: "hvac_install",
            8450: "plumb_demand", 128196: "plumb_maint",
        },
        "costco_bu": [],
        "ropp_tags": [52206586],   # "ROPP" tag (not "Possible ROPP")
        "plumb": True, "costco": False,
    },
    "russett": {
        "tenant": "RUS",
        "tz": "arizona",
        "bu": {
            221: "hvac_demand", 53208412: "hvac_maint", 223: "hvac_sales",
            42371009: "hvac_install", 220: "hvac_install",
        },
        "costco_bu": [],
        "ropp_tags": [63640008],
        "plumb": False, "costco": False,
    },
}

HVAC_SERVICE = ("hvac_demand", "hvac_maint")
PLUMB_SERVICE = ("plumb_demand", "plumb_maint")
PLUMB_ALL = ("plumb_demand", "plumb_maint", "plumb_install")


# ---------------------------------------------------------------- timezones
def _us_dst_bounds(year):
    """(start, end) of US daylight saving time: 2nd Sunday of March 2am -> 1st Sunday of Nov 2am."""
    def nth_sunday(month, n):
        d = dt.date(year, month, 1)
        d += dt.timedelta(days=(6 - d.weekday()) % 7)  # first Sunday
        return d + dt.timedelta(weeks=n - 1)
    return nth_sunday(3, 2), nth_sunday(11, 1)

def _utc_offset_hours(tz, day):
    dst_start, dst_end = _us_dst_bounds(day.year)
    dst = dst_start <= day < dst_end
    if tz == "pacific":
        return -7 if dst else -8
    if tz == "mountain":
        return -6 if dst else -7
    if tz == "arizona":
        return -7
    raise ValueError(tz)

def local_day_window_utc(tz, day):
    """UTC [start, end) covering the tenant-local calendar day."""
    off = _utc_offset_hours(tz, day)
    start = dt.datetime(day.year, day.month, day.day) - dt.timedelta(hours=off)
    end = start + dt.timedelta(days=1)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return start.strftime(fmt), end.strftime(fmt)

def local_today(tz):
    utc_now = dt.datetime.utcnow()
    return (utc_now + dt.timedelta(hours=_utc_offset_hours(tz, utc_now.date()))).date()


# ---------------------------------------------------------------- API paging
def fetch_all(tenant, path, params, page_size=200, max_pages=40, retries=3):
    items, page = [], 1
    while page <= max_pages:
        p = dict(params, pageSize=page_size, page=page)
        last_err = None
        for attempt in range(retries):
            try:
                r = st_get(tenant, path, params=p)
                last_err = None
                break
            except Exception as e:
                last_err = e
                time.sleep(2 * (attempt + 1))
        if last_err is not None:
            raise RuntimeError(f"{path} page {page} failed after {retries} tries: {last_err}")
        items.extend(r.get("data", []))
        if not r.get("hasMore"):
            break
        page += 1
    return items


# ---------------------------------------------------------------- config cache
def _load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default

def _save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    os.replace(tmp, path)

def _cached_names(tenant, kind, path, page_size=100):
    """{id: name} lookup cached on disk (these rarely change)."""
    cache = _load_json(CONFIG_CACHE, {})
    entry = cache.get(f"{tenant}:{kind}")
    if entry and time.time() - entry.get("at", 0) < CONFIG_TTL_HOURS * 3600:
        return {int(k): v for k, v in entry["types"].items()}
    types = {str(j["id"]): (j.get("name") or "") for j in
             fetch_all(tenant, path, {}, page_size=page_size)}
    cache[f"{tenant}:{kind}"] = {"at": time.time(), "types": types}
    _save_json(CONFIG_CACHE, cache)
    return {int(k): v for k, v in types.items()}

def job_type_names(tenant):
    return _cached_names(tenant, "job_types", "/jpm/v2/tenant/{tenant}/job-types")

def membership_type_names(tenant):
    return _cached_names(tenant, "membership_types", "/memberships/v2/tenant/{tenant}/membership-types")


# ---------------------------------------------------------------- classification
def _in_window(ts, start, end):
    return ts is not None and start <= ts < end

def _sales_category(co, job):
    """tgl / costco / mkt for an HVAC-Sales job."""
    name = job["_jt_name"].lower()
    if "tgl" in name:
        return "tgl"
    if job.get("businessUnitId") in co["costco_bu"] or "costco" in name:
        return "costco"
    return "mkt"

def _wh_category(name):
    n = name.lower()
    if "install" not in n:
        return None
    if "tankless" in n:
        return "tankless"
    if "softener" in n or "filtration" in n or "reverse osmosis" in n:
        return "filtration"
    if "tanked water heater" in n or "temp water heater" in n:
        return "tanks"
    return None


# ---------------------------------------------------------------- metric core
def compute_day(company, day, jt_names=None):
    """All dashboard metrics for one company and one local calendar day."""
    co = COMPANIES[company]
    tenant = co["tenant"]
    start, end = local_day_window_utc(co["tz"], day)
    jt_names = jt_names or job_type_names(tenant)
    bu = {int(k): v for k, v in co["bu"].items()}
    ropp_tags = set(co["ropp_tags"])

    def decorate(job):
        job["_bucket"] = bu.get(job.get("businessUnitId"))
        job["_jt_name"] = jt_names.get(job.get("jobTypeId"), "")
        job["_ropp"] = bool(ropp_tags & set(job.get("tagTypeIds") or []))
        n = job["_jt_name"].lower()
        job["_tgl"] = "tgl" in n
        job["_tgl_lead"] = "tgl" in n and n.startswith("estimate")  # "Install ... TGL" is not a set lead
        return job

    # -- jobs with an appointment today (the "board")
    board = [decorate(j) for j in fetch_all(
        tenant, "/jpm/v2/tenant/{tenant}/jobs",
        {"appointmentStartsOnOrAfter": start, "appointmentStartsBefore": end})]
    jobs_by_id = {j["id"]: j for j in board}

    def count(pred):
        return sum(1 for j in board if pred(j))

    live = lambda j: j.get("jobStatus") != "Canceled"
    ran = lambda j: j.get("jobStatus") == "Completed" and _in_window(j.get("completedOn"), start, end)

    m = {}
    m["hvacMaintenanceOnBoard"] = count(lambda j: j["_bucket"] == "hvac_maint" and live(j))
    m["hvacDemandOnBoard"] = count(lambda j: j["_bucket"] == "hvac_demand" and live(j))
    m["hvacJobsOnBoard"] = m["hvacMaintenanceOnBoard"] + m["hvacDemandOnBoard"]
    m["hvacMaintenanceRan"] = count(lambda j: j["_bucket"] == "hvac_maint" and ran(j))
    m["hvacDemandRan"] = count(lambda j: j["_bucket"] == "hvac_demand" and ran(j))
    m["hvacRoppsOnBoard"] = count(lambda j: j["_bucket"] in HVAC_SERVICE and live(j) and j["_ropp"])
    m["hvacRoppsRan"] = count(lambda j: j["_bucket"] in HVAC_SERVICE and ran(j) and j["_ropp"])

    m["plumbMaintenanceOnBoard"] = count(lambda j: j["_bucket"] == "plumb_maint" and live(j))
    m["plumbDemandOnBoard"] = count(lambda j: j["_bucket"] == "plumb_demand" and live(j))
    m["plumbJobsOnBoard"] = m["plumbMaintenanceOnBoard"] + m["plumbDemandOnBoard"]
    m["plumbMaintenanceRan"] = count(lambda j: j["_bucket"] == "plumb_maint" and ran(j))
    m["plumbDemandRan"] = count(lambda j: j["_bucket"] == "plumb_demand" and ran(j))

    m["hvacSMCanceled"] = count(lambda j: j["_bucket"] in HVAC_SERVICE and j.get("jobStatus") == "Canceled")
    m["hvacSalesCanceled"] = count(lambda j: j["_bucket"] == "hvac_sales" and j.get("jobStatus") == "Canceled")
    m["plumbCanceled"] = count(lambda j: (j["_bucket"] or "").startswith("plumb") and j.get("jobStatus") == "Canceled")

    # sales-lead board: leads ran today by category
    leads_ran = {"tgl": 0, "costco": 0, "mkt": 0}
    for j in board:
        if j["_bucket"] == "hvac_sales" and ran(j):
            leads_ran[_sales_category(co, j)] += 1

    # -- TGLs set today (jobs created today with a TGL job type)
    created = [decorate(j) for j in fetch_all(
        tenant, "/jpm/v2/tenant/{tenant}/jobs",
        {"createdOnOrAfter": start, "createdBefore": end})]
    tgl_created = [j for j in created if j["_tgl_lead"] and j.get("jobStatus") != "Canceled"]
    m["tglsSet"] = len(tgl_created)
    m["tglsSetSameDay"] = sum(1 for j in tgl_created if j["id"] in jobs_by_id)

    # plumbing big-ticket installs completed today
    wh = {"tankless": 0, "filtration": 0, "tanks": 0}
    for j in board:
        if ran(j):
            cat = _wh_category(j["_jt_name"])
            if cat:
                wh[cat] += 1
    m["plumbTanklessSold"] = wh["tankless"]
    m["plumbFiltrationSold"] = wh["filtration"]
    m["plumbTanksSold"] = wh["tanks"]

    # -- estimates sold today
    estimates = fetch_all(tenant, "/sales/v2/tenant/{tenant}/estimates",
                          {"soldAfter": start, "soldBefore": end})
    missing = sorted({e["jobId"] for e in estimates if e.get("jobId") and e["jobId"] not in jobs_by_id})
    for i in range(0, len(missing), 50):
        for j in fetch_all(tenant, "/jpm/v2/tenant/{tenant}/jobs",
                           {"ids": ",".join(map(str, missing[i:i + 50]))}):
            jobs_by_id[j["id"]] = decorate(j)

    daily_sales = hvac_service_sales = plumb_sales = 0.0
    cat_sales = {"tgl": 0.0, "costco": 0.0, "mkt": 0.0}
    cat_sold_jobs = {"tgl": set(), "costco": set(), "mkt": set()}
    for e in estimates:
        amt = float(e.get("subtotal") or 0)
        daily_sales += amt
        job = jobs_by_id.get(e.get("jobId"))
        bucket = job["_bucket"] if job else None
        if bucket in HVAC_SERVICE:
            hvac_service_sales += amt
        elif bucket in PLUMB_ALL:
            plumb_sales += amt
        elif bucket == "hvac_sales":
            cat = _sales_category(co, job)
            cat_sales[cat] += amt
            cat_sold_jobs[cat].add(job["id"])

    m["dailySales"] = round(daily_sales, 2)
    m["hvacServiceSales"] = round(hvac_service_sales, 2)
    m["plumbSales"] = round(plumb_sales, 2)
    for cat, label in (("tgl", "tgl"), ("mkt", "mkt"), ("costco", "costco")):
        total, ran_n, sold_n = cat_sales[cat], leads_ran[cat], len(cat_sold_jobs[cat])
        prefix = label
        m[prefix + "TotalSales"] = round(total, 2)
        m[prefix + "LeadsRan"] = ran_n
        m[prefix + "LeadsSold"] = sold_n
        m[prefix + "Conversion"] = round(sold_n / ran_n * 100, 1) if ran_n else 0
        m[prefix + "AvgTicket"] = round(total / sold_n) if sold_n else 0

    # -- invoices dated today (invoiceDate is a calendar date stored as midnight UTC)
    day_s = day.strftime("%Y-%m-%dT00:00:00Z")
    day_e = (day + dt.timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z")
    invoices = fetch_all(tenant, "/accounting/v2/tenant/{tenant}/invoices",
                         {"invoicedOnOrAfter": day_s, "invoicedOnBefore": day_e}, page_size=500)
    daily_rev = hvac_service_rev = plumb_rev = 0.0
    memb = {"hvac": 0, "plumb": 0}
    for inv in invoices:
        sub = float(inv.get("subTotal") or 0)
        daily_rev += sub
        bucket = bu.get((inv.get("businessUnit") or {}).get("id"))
        if bucket in HVAC_SERVICE:
            hvac_service_rev += sub
        elif bucket in PLUMB_ALL:
            plumb_rev += sub
        # memberships sold: distinct invoices carrying a membership line item
        # (multi-system memberships bill one item per system - count the sale once)
        if any((it.get("membershipTypeId") or 0) or it.get("type") == "Membership"
               for it in (inv.get("items") or [])):
            memb["plumb" if (bucket or "").startswith("plumb") else "hvac"] += 1
    m["dailyRevenue"] = round(daily_rev, 2)
    m["hvacServiceRevenue"] = round(hvac_service_rev, 2)
    m["plumbRevenue"] = round(plumb_rev, 2)
    m["hvacMembershipsSold"] = memb["hvac"]
    m["plumbMembershipsSold"] = memb["plumb"]

    # -- phones
    calls = fetch_all(tenant, "/telecom/v2/tenant/{tenant}/calls",
                      {"createdOnOrAfter": start, "createdBefore": end}, page_size=500)
    inbound = booked = unbooked = 0
    for c in calls:
        lc = c.get("leadCall") or {}
        if lc.get("direction") != "Inbound":
            continue
        inbound += 1
        ct = lc.get("callType")
        if ct == "Booked":
            booked += 1
        elif ct in ("Unbooked", "NotBooked"):
            unbooked += 1
    leads = booked + unbooked
    m["callsInbound"] = inbound
    m["inboundLeads"] = leads
    m["bookingRate"] = round(booked / leads * 100, 1) if leads else 0

    m["flipRate"] = round(m["tglsSet"] / m["hvacRoppsRan"] * 100, 1) if m["hvacRoppsRan"] else 0
    return m


# ---------------------------------------------------------------- public API
def compute_current():
    out = {}
    for company, co in COMPANIES.items():
        day = local_today(co["tz"])
        m = compute_day(company, day)
        m["lastUpdated"] = dt.datetime.now().strftime("%a %b %d %Y %H:%M:%S") + " (live API)"
        out[company] = m
    return out


def _history_days(tz, n=HISTORY_WEEKDAYS):
    """Last n weekdays before today (oldest first)."""
    days, d = [], local_today(tz)
    while len(days) < n:
        d -= dt.timedelta(days=1)
        if d.weekday() < 5:
            days.append(d)
    return list(reversed(days))


def read_history():
    """History as currently cached on disk - no API calls (backfill runs separately)."""
    cache = _load_json(HISTORY_FILE, {})
    out = {}
    for company, co in COMPANIES.items():
        wanted = {d.isoformat() for d in _history_days(co["tz"])}
        entries = [e for e in cache.get(company, []) if e["date"] in wanted]
        out[company] = sorted(entries, key=lambda e: e["date"])
    return out


def compute_history(progress=None):
    """Past-weekday metrics per company, cached on disk (past days never change)."""
    cache = _load_json(HISTORY_FILE, {})
    out = {}
    for company, co in COMPANIES.items():
        jt = None
        entries = {e["date"]: e for e in cache.get(company, [])}
        result = []
        for day in _history_days(co["tz"]):
            key = day.isoformat()
            if key not in entries:
                jt = jt or job_type_names(co["tenant"])
                m = compute_day(company, day, jt_names=jt)
                m["date"] = key
                entries[key] = m
                cache[company] = sorted(entries.values(), key=lambda e: e["date"])
                _save_json(HISTORY_FILE, cache)
                if progress:
                    progress(company, key)
            result.append(entries[key])
        out[company] = result
    return out


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    company = sys.argv[1] if len(sys.argv) > 1 else "sierra"
    day = dt.date.fromisoformat(sys.argv[2]) if len(sys.argv) > 2 else local_today(COMPANIES[company]["tz"])
    t0 = time.time()
    print(json.dumps(compute_day(company, day), indent=2))
    print(f"-- {company} {day} in {time.time() - t0:.1f}s", file=sys.stderr)
