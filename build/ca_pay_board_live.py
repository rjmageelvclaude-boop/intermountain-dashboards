#!/usr/bin/env python3
"""
Live ServiceTitan engine for the Comfort Advisor Commission Audit board
(site/ca-pay-board/).

Audits every commission dollar paid to Sierra's Comfort Advisors against what
the pay plan says it should have been, job by job:

  what WAS paid      = payroll gross-pay-items (InvoiceRelatedBonus) whose
                       activity starts with "Commission", grouped by job;
                       "Direct Adjustment" items ride along as manual
                       adjustments (they are often the hand-corrections for
                       the very dings this board audits, so variance is
                       measured on paid + adjustments)
  what SHOULD be paid = base rate x net sale, per the official "Comfort
                       Advisor Pay Policy" (rates by sales category; the
                       category comes from the job type name, except Goodman
                       which is read off the billed equipment):
                         contains "Costco"      -> 7%
                         contains "TGL"/"LTO"   -> 8%   (tech-generated lead)
                         anything else          -> 10%  (straight Sierra)
                         Goodman equipment      -> flat 5%, no ladder
                       ...then dinged by the total discount % off book price
                       (whole points, per the policy):
                         0-5%         no hit
                         6-8%         -1 point
                         9%           -2 points
                         10%+         flat 5%
                       Discount-related pay decisions are at the Sales
                       Manager's discretion - variances are review items.
  discount            = book price (invoice items repriced at today's
                       pricebook, the ca-board scheme) minus net sale, split
                       into explicit discount lines (PriceModifier items,
                       e.g. "7% Discount Equipment", "VIP Discount") and
                       price manipulation (line prices set below book with no
                       discount line to show for it)

Rows are keyed by (job, month of the pay item date) so the board can be cut
by payroll period / MTD / YTD on the same basis the checks actually go out.
Everything ships in one flat jobs array; the page does the windowing and the
department/rep/job rollups client-side.

Payroll pulls use the gm-board _sliced() lesson: gross-pay-items paginate
nondeterministically, so each pull is a 2-day slice small enough for a single
pageSize=5000 page. Closed months freeze into data/ca-pay-board-history.json
45 days past month-end (pay adjustments trickle for weeks).

CLI smoke test:
    py build/ca_pay_board_live.py            # full compute, prints summary
    py build/ca_pay_board_live.py 2026-06    # recompute one month, verbose
"""
import datetime as dt
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from servicetitan_client import st_get
from command_center_live import (fetch_all, local_today, _utc_offset_hours,
                                 _load_json, _save_json)
from ca_board_live import book_prices, _PB_ENDPOINT

DATA_DIR = os.path.join(ROOT, "data")
HIST_PATH = os.path.join(DATA_DIR, "ca-pay-board-history.json")

TENANT = "SIE"
TZ = "pacific"
CA_TEAM = "1ca"              # technicians team, case-insensitive

# ---- the pay plan ("Comfort Advisor Pay Policy" PDF, applied 2026-07-17) ---
# Rates by sales category; Goodman is equipment-based (detected from the
# invoice items - job types don't say Goodman) and pays a flat 5% with no
# discount ladder, since 5% is also the ladder's floor.
BASE_RATES = {"Costco": 0.07, "TGL": 0.08, "Marketed": 0.10, "Goodman": 0.05}
# Discounting rules, in the policy's whole percentage points:
# 0-5% no effect | 6-8% -1pt | 9% -2pts | 10%+ flat OVER_CAP_RATE.
# (All discount-related pay decisions are at the Sales Manager's discretion,
# so a variance is a review item, not automatically an error.)
TIERS = [(5, 0.00), (8, 0.01), (9, 0.02)]
OVER_CAP_RATE = 0.05

FREEZE_DAYS = 45             # month freezes this long after month-end
VARIANCE_FLAG = 1.0          # |paid+adj - expected| beyond this many $ flags
MANIP_FLAG = 50.0            # book - list beyond this many $ flags


def _bucket(job_type_name):
    t = (job_type_name or "").lower()
    if "costco" in t:
        return "Costco"
    if "tgl" in t or "lto" in t:      # RJ 2026-07-17: LTO = TGL, both 8%
        return "TGL"
    return "Marketed"


def _activity_bucket(activity):
    """The bucket the payroll activity SAYS it paid (for mismatch flags)."""
    a = (activity or "").lower()
    if "costco" in a:
        return "Costco"
    if "lto" in a:
        return "TGL"
    return "Marketed"


def expected_rate(bucket, disc_pct):
    base = BASE_RATES[bucket]
    if bucket == "Goodman":
        return base                    # flat 5%, no ladder
    pts = int(disc_pct * 100 + 0.5)    # the policy speaks in whole points
    if pts >= 10:
        return OVER_CAP_RATE
    for ceiling, hit in TIERS:
        if pts <= ceiling:
            return base - hit
    return OVER_CAP_RATE


# ---------------------------------------------------------------- roster
def ca_roster():
    """{techId: {name, active}} - every technician ever on the CA team,
    inactive included (an audit shouldn't lose reps who left mid-year)."""
    out = {}
    for t in fetch_all(TENANT, "/settings/v2/tenant/{tenant}/technicians", {},
                       page_size=200, max_pages=40):
        if (t.get("team") or "").strip().lower() != CA_TEAM:
            continue
        name = (t.get("name") or "").strip()
        if name.upper().endswith("-TECH"):
            name = name[:-5].rstrip(" -")
        out[t["id"]] = {"name": name, "active": bool(t.get("active"))}
    return out


# ---------------------------------------------------------------- pay periods
def pay_periods(today):
    """[{start, end}] tenant-local payroll periods covering the year so far,
    newest first, from the payrolls feed (SIE runs weekly)."""
    jan1 = dt.datetime(today.year, 1, 1) - dt.timedelta(days=16)
    seen = set()
    for p in fetch_all(TENANT, "/payroll/v2/tenant/{tenant}/payrolls",
                       {"startedOnOrAfter": jan1.strftime("%Y-%m-%dT%H:%M:%SZ")},
                       page_size=500, max_pages=40):
        try:
            s = dt.datetime.strptime(p["startedOn"][:19], "%Y-%m-%dT%H:%M:%S")
            e = dt.datetime.strptime(p["endedOn"][:19], "%Y-%m-%dT%H:%M:%S")
        except (KeyError, TypeError, ValueError):
            continue
        start = (s + dt.timedelta(hours=_utc_offset_hours(TZ, s.date()))).date()
        # endedOn is the period's last instant - back off so .date() lands
        # on the last day (the scorecard lesson)
        end = (e + dt.timedelta(hours=_utc_offset_hours(TZ, e.date()))
               - dt.timedelta(seconds=1)).date()
        if end.year >= today.year and start <= today:
            seen.add((start.isoformat(), end.isoformat()))
    return [{"start": s, "end": e}
            for s, e in sorted(seen, reverse=True)]


# ---------------------------------------------------------------- payroll pull
def _gpi_slices(day_from, day_to, log=print):
    """gross-pay-items for tenant-local [day_from, day_to] inclusive, pulled
    in 2-day slices so each slice fits one deterministic 5000-row page."""
    out, cur = [], day_from
    while cur <= day_to:
        end = min(cur + dt.timedelta(days=1), day_to)
        params = {"dateOnOrAfter": cur.isoformat(),
                  "dateOnOrBefore": (end + dt.timedelta(days=1)).isoformat()}
        page = 1
        while True:
            r = st_get(TENANT, "/payroll/v2/tenant/{tenant}/gross-pay-items",
                       params=dict(params, pageSize=5000, page=page))
            out.extend(r.get("data") or [])
            if not r.get("hasMore"):
                break
            page += 1
        cur = end + dt.timedelta(days=1)
    return out


# ---------------------------------------------------------------- batched gets
def _by_ids(path, ids, page_size=50):
    out = {}
    ids = sorted(set(ids))
    for i in range(0, len(ids), page_size):
        batch = ",".join(str(x) for x in ids[i:i + page_size])
        for row in fetch_all(TENANT, path, {"ids": batch},
                             page_size=page_size, max_pages=3):
            out[row["id"]] = row
    return out


def _job_types():
    return {t["id"]: t["name"]
            for t in fetch_all(TENANT, "/jpm/v2/tenant/{tenant}/job-types", {},
                               page_size=200, max_pages=20)}


# ---------------------------------------------------------------- invoices
# Commission clawbacks are computed off EVERYTHING billed on the job - the
# office books "Write off-Per RJ" adjustment invoices that never appear on
# the pay item's own invoice - so the store keeps every active invoice per
# job: backfilled once per new job, then delta-synced via modifiedOnOrAfter
# (a write-off added weeks later still lands on the job it corrects).
SYNC_OVERLAP_MIN = 30

def _slim_invoice(v):
    items = []
    goodman = 0.0
    for it in (v.get("items") or []):
        typ, tot = it.get("type"), float(it.get("total") or 0)
        if typ == "PriceModifier" or typ in _PB_ENDPOINT:
            nm = (it.get("displayName") or it.get("skuName") or "").strip()
            if tot > 0 and "goodman" in nm.lower():
                goodman += tot         # Goodman sales pay their own flat rate
            if typ != "PriceModifier" and tot >= 0:
                nm = ""                # names only kept for discount lines
            items.append([typ, it.get("skuId") or 0,
                          float(it.get("quantity") or 0), tot, nm])
    return {"sub": float(v.get("subTotal") or 0), "items": items,
            "gm": round(goodman, 2)}


INV_STORE_VERSION = 2                  # bump when _slim_invoice changes


def sync_invoices(store, job_ids, log=print):
    """Bring store["byJob"] = {jobId: {invoiceId: slim}} current for job_ids."""
    if store.get("iv") != INV_STORE_VERSION:   # slim format changed - refetch
        store.clear()
        store["iv"] = INV_STORE_VERSION
    by_job = store.setdefault("byJob", {})
    now = dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    new = [j for j in job_ids if str(j) not in by_job]
    if new:
        from concurrent.futures import ThreadPoolExecutor
        def one(jid):
            rows = fetch_all(TENANT, "/accounting/v2/tenant/{tenant}/invoices",
                             {"jobId": jid}, page_size=100, max_pages=3)
            return jid, {str(v["id"]): _slim_invoice(v)
                         for v in rows if v.get("active", True)}
        with ThreadPoolExecutor(max_workers=8) as pool:
            for jid, invs in pool.map(one, new):
                by_job[str(jid)] = invs
        log(f"invoices backfilled for {len(new)} new jobs")

    since = store.get("synced")
    if since:
        since = _minus_minutes(since, SYNC_OVERLAP_MIN)
        want = {str(j) for j in job_ids}
        touched = 0
        for v in fetch_all(TENANT, "/accounting/v2/tenant/{tenant}/invoices",
                           {"modifiedOnOrAfter": since}, page_size=500,
                           max_pages=200):
            jid = str(((v.get("job") or {}).get("id")) or "")
            if jid in want:
                invs = by_job.setdefault(jid, {})
                if v.get("active", True):
                    invs[str(v["id"])] = _slim_invoice(v)
                else:
                    invs.pop(str(v["id"]), None)
                touched += 1
        if touched:
            log(f"invoice delta: {touched} rows on audited jobs")
    store["synced"] = now


def _minus_minutes(ts, minutes):
    t = dt.datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")
    return (t - dt.timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def price_job(invs, prices):
    """Reprice one job's invoices at today's book (ca-board scheme):
    PriceModifier lines are explicit discount codes; negative service lines
    are write-offs/credits; unpriceable SKUs fall back to their sold total so
    they can never fake a discount."""
    p = {"sold": 0.0, "list": 0.0, "book": 0.0, "discLines": 0.0,
         "writeOff": 0.0, "goodman": 0.0, "discNames": []}
    for v in invs.values():
        p["sold"] += v["sub"]
        p["goodman"] += v.get("gm", 0.0)
        for typ, sku, qty, tot, nm in v["items"]:
            if typ == "PriceModifier":
                p["discLines"] += -tot             # stored negative
            elif tot < 0:
                p["writeOff"] += -tot              # write-off / credit line
            else:
                p["list"] += tot
                price = prices.get((typ, sku)) if sku else None
                if price is not None and price > 0 and qty > 0:
                    p["book"] += qty * price
                else:
                    p["book"] += tot
                continue
            if nm and nm not in p["discNames"]:
                p["discNames"].append(nm)
    return p


def month_items(year, month, roster, log=print):
    """Slim CA commission/adjustment pay items whose pay date is in the month:
    [jobId, empId, activity, amount, date, invoiceId, memo]. jobId 0 = not
    tied to a job (shown separately, audited by hand)."""
    today = local_today(TZ)
    m_start = dt.date(year, month, 1)
    m_end = (dt.date(year + (month == 12), month % 12 + 1, 1)
             - dt.timedelta(days=1))
    gpi = _gpi_slices(m_start, min(m_end, today), log=log)

    out = []
    for i in gpi:
        emp = i.get("employeeId") or i.get("technicianId")
        if emp not in roster or i.get("grossPayItemType") != "InvoiceRelatedBonus":
            continue
        act = (i.get("activity") or "").strip()
        if not (act.lower().startswith("commission") or act == "Direct Adjustment"):
            continue
        amt = float(i.get("amount") or 0)
        if not amt and not i.get("jobId"):
            continue
        out.append([i.get("jobId") or 0, emp, act, amt,
                    (i.get("date") or "")[:10], i.get("invoiceId") or 0,
                    (i.get("memo") or "").strip()])
    log(f"{year}-{month:02d}: {len(out)} CA pay items")
    return out


def build_rows(items, roster, jt_names, inv_store, log=print):
    """Group pay items per JOB across the whole year - a job's commission and
    its clawback often post in different pay periods, and scoring them apart
    double-counts the plan. Each job is dated by its commission pay date so
    the board windows on the period the commission actually went out."""
    comm, unlinked = {}, []
    for jid, emp, act, amt, date, inv, memo in items:
        is_comm = act != "Direct Adjustment"
        if not jid:
            unlinked.append({"repId": emp, "rep": roster[emp]["name"],
                             "date": date, "amount": round(amt, 2),
                             "memo": memo})
            continue
        row = comm.setdefault(jid, {
            "paidBy": {}, "adjBy": {}, "activities": [],
            "commDates": [], "adjDates": []})
        by = row["paidBy"] if is_comm else row["adjBy"]
        by[emp] = by.get(emp, 0.0) + amt
        (row["commDates"] if is_comm else row["adjDates"]).append(date)
        if is_comm and act not in row["activities"]:
            row["activities"].append(act)

    jobs = _by_ids("/jpm/v2/tenant/{tenant}/jobs", comm.keys())
    by_job = inv_store.get("byJob", {})
    recent_cut = (local_today(TZ) - dt.timedelta(days=30)).isoformat()

    wanted = set()
    for jid in comm:
        for v in by_job.get(str(jid), {}).values():
            for typ, sku, _q, tot, _n in v["items"]:
                if typ in _PB_ENDPOINT and sku and tot >= 0:
                    wanted.add((typ, sku))
    prices = book_prices(TENANT, wanted, log=log)

    rows = []
    for jid, r in comm.items():
        job = jobs.get(jid) or {}
        jt = jt_names.get(job.get("jobTypeId"), "")
        invs = by_job.get(str(jid), {})
        p = price_job(invs, prices)
        bucket = _bucket(jt)
        # Goodman sales pay their own flat rate regardless of lead source -
        # it's an equipment attribute, so detect it from what was billed
        if bucket != "Costco" and p["goodman"] > 0.5 * max(p["list"], 1):
            bucket = "Goodman"

        paid = sum(r["paidBy"].values())
        adj = sum(r["adjBy"].values())
        adj_only = not r["paidBy"]
        disc_pct = (max(0.0, p["book"] - p["sold"]) / p["book"]
                    if p["book"] > 0 else 0.0)
        rate = expected_rate(bucket, disc_pct)
        expected = rate * p["sold"]

        flags = []
        if adj_only:
            # a correction for a commission paid outside this year - real
            # money this year, but there is no in-year plan to score against
            flags.append("adj-only")
            expected = 0.0
        elif paid <= 0:
            # net-reversed commission (sale canceled or re-issued elsewhere) -
            # surface it, but don't score a plan against a dead sale
            flags.append("reversal")
            expected = 0.0
        elif not invs or p["sold"] <= 0:
            # commission with no priceable sale behind it is its own finding
            flags.append("no-invoice")
            expected = 0.0
        variance = (paid + adj) - expected

        if not adj_only and paid > 0:
            if variance > VARIANCE_FLAG:
                flags.append("overpaid")
            elif variance < -VARIANCE_FLAG:
                flags.append("underpaid")
        if p["book"] - p["list"] > MANIP_FLAG:
            flags.append("price-manip")
        if int(disc_pct * 100 + 0.5) >= 10:
            flags.append("disc>10")
        acts = {_activity_bucket(a) for a in r["activities"]}
        if bucket != "Goodman" and acts and acts != {bucket}:
            flags.append("activity-mismatch")   # payroll paid the wrong category
        if len(r["paidBy"]) > 1:
            flags.append("multi-rep")
        # clawbacks post weeks after the commission - a fresh overpay may
        # simply not be processed yet, so let the page soften it
        if ("overpaid" in flags and r["commDates"]
                and min(r["commDates"]) >= recent_cut):
            flags.append("recent")

        payers = r["paidBy"] or r["adjBy"]
        rep_id = max(payers, key=lambda e: abs(payers[e]))
        rows.append({
            "id": jid,
            "date": min(r["commDates"] or r["adjDates"]),
            "completedOn": (job.get("completedOn") or "")[:10],
            "repId": rep_id,
            "rep": roster[rep_id]["name"],
            "reps": [{"id": e, "name": roster[e]["name"],
                      "paid": round(a, 2)} for e, a in r["paidBy"].items()],
            "type": jt,
            "bucket": bucket,
            "activities": r["activities"],
            "book": round(p["book"], 2),
            "list": round(p["list"], 2),
            "sold": round(p["sold"], 2),
            "discLines": round(p["discLines"], 2),
            "writeOff": round(p["writeOff"], 2),
            "discNames": p["discNames"],
            "discPct": round(disc_pct, 4),
            "baseRate": BASE_RATES[bucket],
            "expRate": rate,
            "expected": round(expected, 2),
            "paid": round(paid, 2),
            "adj": round(adj, 2),
            "variance": round(variance, 2),
            "flags": flags,
        })
    rows.sort(key=lambda r: -r["variance"])
    log(f"{len(rows)} commission jobs, {len(unlinked)} unlinked adjustments")
    return rows, unlinked


# ---------------------------------------------------------------- compute
def compute(log=print):
    today = local_today(TZ)
    hist = _load_json(HIST_PATH, {})
    if hist.get("year") != today.year or hist.get("v") != 2:
        hist = {"year": today.year, "v": 2, "months": {}}

    roster = ca_roster()
    jt_names = _job_types()
    periods = pay_periods(today)

    # raw pay items are cached per month (and frozen 45 days past month-end);
    # jobs/invoices/pricing are re-resolved fresh every run so late-arriving
    # clawbacks always merge onto the job they correct
    items = []
    for month in range(1, today.month + 1):
        key = f"{today.year}-{month:02d}"
        got = hist["months"].get(key)
        if got and got.get("frozen"):
            items += got["items"]
            continue
        mi = month_items(today.year, month, roster, log=log)
        m_end = (dt.date(today.year + (month == 12), month % 12 + 1, 1)
                 - dt.timedelta(days=1))
        hist["months"][key] = {"items": mi,
                               "frozen": (today - m_end).days > FREEZE_DAYS}
        _save_json(HIST_PATH, hist)   # each month resumes if the run dies
        items += mi

    inv_store = hist.setdefault("invoices", {})
    sync_invoices(inv_store, {it[0] for it in items if it[0]}, log=log)
    _save_json(HIST_PATH, hist)

    all_rows, all_unlinked = build_rows(items, roster, jt_names, inv_store,
                                        log=log)

    return {
        "generatedAt": dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "updated": dt.datetime.utcnow().strftime("%b %d, %H:%M UTC"),
        "year": today.year,
        "rates": BASE_RATES,
        "tiers": TIERS,
        "overCapRate": OVER_CAP_RATE,
        "payPeriods": periods,
        "reps": [{"id": i, **r} for i, r in sorted(
            roster.items(), key=lambda kv: kv[1]["name"])],
        "jobs": all_rows,
        "unlinkedAdjustments": all_unlinked,
    }


if __name__ == "__main__":
    if len(sys.argv) > 1:                      # py ... 2026-06 -> one month
        y, m = map(int, sys.argv[1].split("-"))
        roster = ca_roster()
        mi = month_items(y, m, roster)
        store = {}
        sync_invoices(store, {it[0] for it in mi if it[0]})
        rows, unlinked = build_rows(mi, roster, _job_types(), store)
        for r in rows:
            print(f"  {r['date']} job {r['id']} {r['rep']:<22} {r['bucket']:<9}"
                  f" sold ${r['sold']:>10,.0f} disc {r['discPct']*100:5.1f}%"
                  f" exp {r['expRate']*100:4.1f}% ${r['expected']:>9,.2f}"
                  f" paid ${r['paid']:>9,.2f} adj ${r['adj']:>8,.2f}"
                  f" var ${r['variance']:>9,.2f}  {','.join(r['flags'])}")
    else:
        data = compute()
        js = data["jobs"]
        over = [j for j in js if "overpaid" in j["flags"]]
        under = [j for j in js if "underpaid" in j["flags"]]
        print(f"\n{len(js)} jobs | expected ${sum(j['expected'] for j in js):,.0f}"
              f" | paid+adj ${sum(j['paid'] + j['adj'] for j in js):,.0f}"
              f" | variance ${sum(j['variance'] for j in js):+,.0f}")
        print(f"overpaid: {len(over)} jobs ${sum(j['variance'] for j in over):+,.0f}"
              f" | underpaid: {len(under)} jobs "
              f"${sum(j['variance'] for j in under):+,.0f}")
