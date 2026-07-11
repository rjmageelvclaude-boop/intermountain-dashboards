#!/usr/bin/env python3
"""
Live ServiceTitan engine for the Comfort Advisor board (site/ca-board/).

Ranks each company's active Comfort Advisors by sold-estimate revenue, MTD and
YTD, with the supporting KPIs from RJ's reference design:

  - Sales           = sum of estimate subtotals soldBy the CA, soldOn in window
  - Book Price Opps = those same estimates re-priced at today's pricebook price
                      (qty x book price per item; custom/discount items fall
                      back so they never inflate the discount)
  - Avg Discount    = (book - sales) / book, floored at 0
  - Opportunities   = completed jobs the CA ran in the sales-side BUs in the
                      window that either charged (!noCharge OR total >=
                      soldThreshold, the call-board rule) or carry an active
                      DECIDED estimate (Sold or Dismissed). Runs whose only
                      estimates are still Open don't count against the close
                      rate yet. Validated against RJ's reference screenshot:
                      sales match to the dollar, opportunity denominators
                      exactly (284/271/240/256/41), closed within 0-2.
  - Closed          = those jobs where the CA has a sold estimate
  - Closed Avg Sale = Sales / Closed
  - Avg Close Rate  = Closed / Opportunities

Rosters come straight from the technicians API: active techs on team
"1CA" (Sierra), "2- Sales" (Ultimate), "Sales" (Russett).

Recomputing YTD from scratch every run would hammer the API, so each tenant
keeps an incremental cache (data/ca-board-cache-<TENANT>.json): jobs and sold
estimates backfill once from Jan 1, then sync deltas via modifiedOnOrAfter.
Appointment->technician assignments are fetched only for sales-BU jobs and
re-checked for the last ASSIGN_RECHECK_DAYS days to catch reassignments.

CLI smoke test:
    py build/ca_board_live.py sierra
"""
import datetime as dt
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from command_center_live import (fetch_all, local_day_window_utc, local_today,
                                 _load_json, _save_json)
from call_board_live import job_type_thresholds, is_opportunity

DATA_DIR = os.path.join(ROOT, "data")

COMPANIES = {
    "sierra":   {"tenant": "SIE", "tz": "pacific",  "team": "1CA",
                 "label": "Sierra",   "color": "#24408e",
                 "sales_bus": [370, 340802904]},          # HVAC Sales + Costco
    "ultimate": {"tenant": "ULT", "tz": "mountain", "team": "2- Sales",
                 "label": "Ultimate", "color": "#6d28d9",
                 "sales_bus": [2693]},
    "russett":  {"tenant": "RUS", "tz": "arizona",  "team": "Sales",
                 "label": "Russett",  "color": "#b91c1c",
                 "sales_bus": [223]},
}

ASSIGN_RECHECK_DAYS = 14     # re-pull assignments for recently completed jobs
PRICEBOOK_TTL_DAYS = 7       # how long a cached book price is trusted
SYNC_OVERLAP_MIN = 30        # re-read this much before the last sync mark

FAR_FUTURE = "9999-01-01T00:00:00Z"


def _cache_path(tenant):
    return os.path.join(DATA_DIR, f"ca-board-cache-{tenant}.json")


def _pricebook_path(tenant):
    return os.path.join(DATA_DIR, f"ca-board-pricebook-{tenant}.json")


def _year_start_utc(tz):
    today = local_today(tz)
    return local_day_window_utc(tz, dt.date(today.year, 1, 1))[0]


def _month_start_utc(tz):
    today = local_today(tz)
    return local_day_window_utc(tz, today.replace(day=1))[0]


def _now_utc_str():
    return dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _minus_minutes(ts, minutes):
    t = dt.datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")
    return (t - dt.timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _plus_days(ts, days):
    t = dt.datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")
    return (t + dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


BACKFILL_CHUNK_DAYS = 30     # backfills go in resumable slices this wide


# ---------------------------------------------------------------- roster
def roster(company):
    """Active technicians on the company's Comfort Advisor team."""
    co = COMPANIES[company]
    techs = fetch_all(co["tenant"], "/settings/v2/tenant/{tenant}/technicians", {})
    want = co["team"].strip().lower()
    out = []
    for t in techs:
        if t.get("active") and (t.get("team") or "").strip().lower() == want:
            name = (t.get("name") or "").strip()
            if name.upper().endswith("-TECH"):        # "Ryan Hernlund-TECH"
                name = name[:-5].rstrip(" -")
            out.append({"id": t["id"], "name": name,
                        "buId": t.get("businessUnitId")})
    return out


def bu_names(tenant):
    return {b["id"]: (b.get("name") or "")
            for b in fetch_all(tenant, "/settings/v2/tenant/{tenant}/business-units",
                               {"active": "Any"}, page_size=100)}


# ---------------------------------------------------------------- job slimming
def _slim_job(j):
    appts = [a for a in (j.get("firstAppointmentId"), j.get("lastAppointmentId")) if a]
    return {
        "bu": j.get("businessUnitId"),
        "status": j.get("jobStatus"),
        "completedOn": j.get("completedOn"),
        "noCharge": bool(j.get("noCharge")),
        "total": float(j.get("total") or 0),
        "jobTypeId": j.get("jobTypeId"),
        "appts": sorted(set(appts)),
    }


def _slim_estimate(e):
    status = (e.get("status") or {}).get("name")
    return {
        "jobId": e.get("jobId"),
        "soldBy": e.get("soldBy"),
        "soldOn": e.get("soldOn"),
        "status": status,
        "active": bool(e.get("active")),
        "subtotal": float(e.get("subtotal") or 0),
        # per item: [skuId, skuType, qty, soldTotal]; only sold estimates need
        # their items (book-price math) - unsold ones just flag the job as an
        # opportunity, so keep the cache slim
        "items": [[(i.get("sku") or {}).get("id"), (i.get("sku") or {}).get("type"),
                   float(i.get("qty") or 0), float(i.get("total") or 0)]
                  for i in (e.get("items") or [])] if status == "Sold" else [],
    }


# ---------------------------------------------------------------- cache sync
def sync_cache(company, log=print):
    """Bring the tenant cache up to date; returns the cache dict."""
    co = COMPANIES[company]
    tenant, tz = co["tenant"], co["tz"]
    sales_bus = set(co["sales_bus"])
    path = _cache_path(tenant)
    cache = _load_json(path, {})
    jobs = cache.setdefault("jobs", {})
    assign = cache.setdefault("assign", {})
    ests = cache.setdefault("estimates", {})
    sync = cache.setdefault("sync", {})

    ytd_start = _year_start_utc(tz)
    now = _now_utc_str()

    # A cache backfilled for a previous year starts over.
    if sync.get("year_start") != ytd_start:
        jobs.clear(); assign.clear(); ests.clear()
        sync.clear()
        sync["year_start"] = ytd_start

    # ---- jobs: backfill once (completed YTD, in resumable 30-day slices so a
    # killed CI run resumes instead of starting over), then modifiedOnOrAfter deltas
    if not sync.get("jobs_synced"):
        sync.setdefault("jobs_bf_started", now)
        frm = sync.get("jobs_bf_from") or ytd_start
        while frm < now:
            to = min(_plus_days(frm, BACKFILL_CHUNK_DAYS), FAR_FUTURE)
            pulled = fetch_all(tenant, "/jpm/v2/tenant/{tenant}/jobs",
                               {"completedOnOrAfter": frm, "completedBefore": to},
                               page_size=200, max_pages=400)
            for j in pulled:
                if j.get("businessUnitId") in sales_bus:
                    jobs[str(j["id"])] = _slim_job(j)
            frm = sync["jobs_bf_from"] = to
            _save_json(path, cache)
            log(f"[{tenant}] jobs backfill thru {to[:10]}: {len(jobs)} kept")
        sync["jobs_synced"] = _minus_minutes(sync.pop("jobs_bf_started"), SYNC_OVERLAP_MIN)
        sync.pop("jobs_bf_from", None)
    else:
        since = _minus_minutes(sync["jobs_synced"], SYNC_OVERLAP_MIN)
        pulled = fetch_all(tenant, "/jpm/v2/tenant/{tenant}/jobs",
                           {"modifiedOnOrAfter": since}, page_size=200,
                           max_pages=200)
        changed = 0
        for j in pulled:
            key = str(j["id"])
            if j.get("businessUnitId") in sales_bus:
                jobs[key] = _slim_job(j)
                changed += 1
            elif key in jobs:            # moved out of a sales BU
                del jobs[key]
                assign.pop(key, None)
        if changed:
            log(f"[{tenant}] jobs delta: {changed} upserted")
        sync["jobs_synced"] = now

    # ---- estimates: backfill once (created YTD + sold YTD, so December
    # estimates sold in January are included; resumable slices like jobs),
    # then modifiedOnOrAfter deltas
    if not sync.get("ests_synced") or sync.get("est_scope") != "all":
        if sync.get("est_scope") != "all":
            ests.clear()
            sync.pop("ests_synced", None)
            sync.pop("ests_bf_from", None)
            sync.pop("ests_bf_sold_from", None)
            sync["est_scope"] = "all"
        sync.setdefault("ests_bf_started", now)
        for mark, param_lo, param_hi in (("ests_bf_from", "createdOnOrAfter", "createdBefore"),
                                         ("ests_bf_sold_from", "soldAfter", "soldBefore")):
            frm = sync.get(mark) or ytd_start
            while frm < now:
                to = min(_plus_days(frm, BACKFILL_CHUNK_DAYS), FAR_FUTURE)
                pulled = fetch_all(tenant, "/sales/v2/tenant/{tenant}/estimates",
                                   {param_lo: frm, param_hi: to},
                                   page_size=200, max_pages=600)
                for e in pulled:
                    ests[str(e["id"])] = _slim_estimate(e)
                frm = sync[mark] = to
                _save_json(path, cache)
                log(f"[{tenant}] estimates backfill ({param_lo}) thru {to[:10]}: {len(ests)}")
        sync["ests_synced"] = _minus_minutes(sync.pop("ests_bf_started"), SYNC_OVERLAP_MIN)
        sync.pop("ests_bf_from", None)
        sync.pop("ests_bf_sold_from", None)
    else:
        since = _minus_minutes(sync["ests_synced"], SYNC_OVERLAP_MIN)
        pulled = fetch_all(tenant, "/sales/v2/tenant/{tenant}/estimates",
                           {"modifiedOnOrAfter": since, "active": "Any"},
                           page_size=200, max_pages=200)
        for e in pulled:
            ests[str(e["id"])] = _slim_estimate(e)
        if pulled:
            log(f"[{tenant}] estimates delta: {len(pulled)} upserted")
        sync["ests_synced"] = now

    # ---- assignments: fetch for jobs without one + recent completions
    recheck_after = (dt.datetime.utcnow()
                     - dt.timedelta(days=ASSIGN_RECHECK_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    need = {}
    for jid, j in jobs.items():
        if not j["appts"]:
            continue
        if jid not in assign or (j.get("completedOn") or "") >= recheck_after:
            need[jid] = j["appts"]
    if need:
        appt_to_job = {}
        for jid, appts in need.items():
            for a in appts:
                appt_to_job[a] = jid
        appt_ids = sorted(appt_to_job)
        fresh = {jid: set() for jid in need}
        for i in range(0, len(appt_ids), 40):
            batch = ",".join(str(a) for a in appt_ids[i:i + 40])
            rows = fetch_all(tenant, "/dispatch/v2/tenant/{tenant}/appointment-assignments",
                             {"appointmentIds": batch}, page_size=200, max_pages=10)
            for a in rows:
                if not a.get("active"):
                    continue
                jid = appt_to_job.get(a.get("appointmentId")) or str(a.get("jobId"))
                if jid in fresh:
                    fresh[jid].add(a["technicianId"])
        for jid, techs in fresh.items():
            assign[jid] = sorted(techs)
        log(f"[{tenant}] assignments refreshed for {len(need)} jobs "
            f"({(len(appt_ids) + 39) // 40} calls)")

    _save_json(path, cache)
    return cache


# ---------------------------------------------------------------- book prices
_PB_ENDPOINT = {
    "Service": "/pricebook/v2/tenant/{tenant}/services",
    "Material": "/pricebook/v2/tenant/{tenant}/materials",
    "Equipment": "/pricebook/v2/tenant/{tenant}/equipment",
}


def book_prices(tenant, wanted, log=print):
    """{(type, skuId): book price or None}; cached on disk with a TTL.
    None means the SKU couldn't be priced (fall back to the sold total)."""
    path = _pricebook_path(tenant)
    cache = _load_json(path, {})
    cutoff = dt.datetime.utcnow().timestamp() - PRICEBOOK_TTL_DAYS * 86400
    out, missing = {}, {}
    for typ, sid in wanted:
        key = f"{typ}:{sid}"
        hit = cache.get(key)
        if hit and hit.get("at", 0) > cutoff:
            out[(typ, sid)] = hit.get("price")
        else:
            missing.setdefault(typ, set()).add(sid)
    fetched = 0
    for typ, ids in missing.items():
        endpoint = _PB_ENDPOINT.get(typ)
        found = {}
        if endpoint:
            ids_list = sorted(ids)
            for i in range(0, len(ids_list), 50):
                batch = ",".join(str(x) for x in ids_list[i:i + 50])
                for row in fetch_all(tenant, endpoint, {"ids": batch, "active": "Any"},
                                     page_size=50, max_pages=3):
                    found[row["id"]] = float(row.get("price") or 0)
        now_ts = dt.datetime.utcnow().timestamp()
        for sid in ids:
            price = found.get(sid)          # None -> unpriceable, remembered too
            cache[f"{typ}:{sid}"] = {"price": price, "at": now_ts}
            out[(typ, sid)] = price
            fetched += 1
    if fetched:
        log(f"[{tenant}] pricebook: {fetched} SKUs refreshed")
        _save_json(path, cache)
    return out


def estimate_book_price(est, prices):
    """Estimate total if every item sold at today's book price.
    Discount items count as 0 book; unpriceable items fall back to sold total."""
    total = 0.0
    for sku_id, sku_type, qty, sold_total in est["items"]:
        if sku_type == "Discount" or (sold_total < 0 and sku_type not in _PB_ENDPOINT):
            continue
        price = prices.get((sku_type, sku_id)) if sku_id else None
        if price is not None and price > 0 and qty > 0:
            total += qty * price
        else:
            total += sold_total
    return total


# ---------------------------------------------------------------- compute
def _window_metrics(advisor_ids, cache, thresholds, start, prices):
    """{techId: metrics} for one [start, now) window."""
    ests = cache["estimates"]
    jobs = cache["jobs"]
    assign = cache["assign"]

    sold_by_tech = {t: [] for t in advisor_ids}      # estimates sold in window
    closer = {}                                       # jobId -> {techIds with a sold est}
    decided = set()                                   # jobIds with a non-Open active estimate
    for e in ests.values():
        if not e["active"]:
            continue
        if e["jobId"] and e["status"] != "Open":
            decided.add(str(e["jobId"]))
        if e["status"] != "Sold" or not e["soldBy"]:
            continue
        if e["soldBy"] in sold_by_tech:
            if e["jobId"]:
                closer.setdefault(str(e["jobId"]), set()).add(e["soldBy"])
            if (e["soldOn"] or "") >= start:
                sold_by_tech[e["soldBy"]].append(e)

    out = {}
    for tech in advisor_ids:
        sold = sold_by_tech[tech]
        sales = sum(e["subtotal"] for e in sold)
        book = sum(estimate_book_price(e, prices) for e in sold)
        discount = max(0.0, (book - sales) / book) if book > 0 else 0.0

        opps = closed = 0
        for jid, j in jobs.items():
            if j["status"] != "Completed" or (j.get("completedOn") or "") < start:
                continue
            if tech not in assign.get(jid, ()):
                continue
            if jid not in decided and not is_opportunity(
                    {"noCharge": j["noCharge"], "total": j["total"],
                     "jobTypeId": j["jobTypeId"]}, thresholds):
                continue
            opps += 1
            if tech in closer.get(jid, ()):
                closed += 1

        out[tech] = {
            "sales": round(sales, 2),
            "bookPrice": round(book, 2),
            "avgDiscount": round(discount, 4),
            "closed": closed,
            "opps": opps,
            "closedAvgSale": round(sales / closed, 2) if closed else 0,
            "closeRate": round(closed / opps, 4) if opps else 0,
        }
    return out


def compute_company(company, log=print):
    co = COMPANIES[company]
    tenant, tz = co["tenant"], co["tz"]

    advisors = roster(company)
    cache = sync_cache(company, log=log)
    thresholds = job_type_thresholds(tenant)
    names = bu_names(tenant)

    # every SKU on any sold estimate by a roster CA (both windows share YTD set)
    adv_ids = {a["id"] for a in advisors}
    wanted = set()
    for e in cache["estimates"].values():
        if e["status"] == "Sold" and e["active"] and e["soldBy"] in adv_ids:
            for sku_id, sku_type, _qty, _tot in e["items"]:
                if sku_id and sku_type in _PB_ENDPOINT:
                    wanted.add((sku_type, sku_id))
    prices = book_prices(tenant, wanted, log=log)

    periods = {}
    for key, start in (("mtd", _month_start_utc(tz)), ("ytd", _year_start_utc(tz))):
        metrics = _window_metrics(adv_ids, cache, thresholds, start, prices)
        rows = []
        for a in advisors:
            m = metrics[a["id"]]
            rows.append({"id": a["id"], "name": a["name"],
                         "dept": names.get(a["buId"], ""), **m})
        rows.sort(key=lambda r: (-r["sales"], -r["opps"], r["name"]))
        for i, r in enumerate(rows, 1):
            r["rank"] = i
        periods[key] = {"start": start, "advisors": rows}

    return {"label": co["label"], "color": co["color"], "periods": periods}


def compute(log=print):
    out = {"generatedAt": _now_utc_str(), "companies": {}}
    for company in COMPANIES:
        log(f"== {company} ==")
        out["companies"][company] = compute_company(company, log=log)
    return out


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else None
    if which:
        result = {which: compute_company(which)}
    else:
        result = compute()["companies"]
    for co, block in result.items():
        for period, p in block["periods"].items():
            print(f"\n--- {co} {period.upper()} (since {p['start']}) ---")
            for r in p["advisors"]:
                print(f"  #{r['rank']:<2} {r['name']:<28} sales ${r['sales']:>12,.0f}"
                      f"  book ${r['bookPrice']:>12,.0f}  disc {r['avgDiscount']*100:4.1f}%"
                      f"  {r['closed']}/{r['opps']} closed  avg ${r['closedAvgSale']:>9,.0f}"
                      f"  rate {r['closeRate']*100:4.0f}%")
