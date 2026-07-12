#!/usr/bin/env python3
"""
Live ServiceTitan engine for the Memberships Board.

Built around the Leadership Summit membership belief system - members carry
~5x lifetime value, so the board tracks the three levers the playbook says
to manage: SELL (new memberships, stack-ranked by Tech and by CSR/office),
ACTIVATE (system checks completed - 1+ check is worth ~4x retention), and
RETAIN (monthly auto-pay mix, cancels, net growth) - for Sierra, Ultimate
and Russett, plus a combined board.

Memberships come straight from /memberships/v2 (the system of record):

  sold          real memberships created in the window. "Real" = the
                membership type's name passes the per-tenant filter below
                (labor warranties are membership-type records in ST but are
                not maintenance plans - they are excluded everywhere).
  soldBilling   billing mix of those sales (Monthly / Annual / OneTime...);
                the playbook's #1 retention lever is monthly auto-pay.
  canceled      real memberships whose cancellationDate falls in the window
                (found via modifiedOnOrAfter - a cancel touches the record).
  checksWon     recurring-service events (system checks) that reached
                status Won in the window, by modifiedOn. Won events always
                carry the jobId of the visit that closed them.
  jobsCompleted completed jobs in the window (any BU) - the "every customer
                every time" denominator: attachRate = sold / jobs * 100.
  sellers       per-seller sold counts via soldById. Technician ids and
                employee ids never collide, so sellers split cleanly into
                the Tech leaderboard and the CSR/Office leaderboard.
  byBU          sales by the membership record's selling business unit.
  totalRev      invoice subTotal (same revenue definition as the Command
                Center) summed over the calendar month.
  memberRev     the slice of totalRev where the customer was already a
                member BEFORE the invoice date (a membership sold with the
                job does not count its own visit as member revenue).
  crossRev      the slice of memberRev billed in a different trade than
                any of the member's plans were sold in - the playbook's
                "memberships are the glue between service lines" number.
  nonMemberJobs completed jobs whose customer was not a member before the
  offered       job's completion day, and how many of those got an offer:
                a membership SKU on any estimate created that month for the
                job, or a membership created for that customer the same
                day. missed offers = nonMemberJobs - offered; the playbook
                target is a 100% offer rate, every customer every time.

Snapshot metrics (active base, billing mix of the base, plan mix) are
recomputed fresh every run from a status=Active fetch - they are cheap
(~10k rows for Sierra in ~3s) and are point-in-time by nature.

Closed months are cached in data/member-board-history.json and recomputed
at most daily until 10 days past month-end, then frozen - same lifecycle
as the other boards.

CLI smoke test:
    py build/member_board_live.py                 # all companies, current month
    py build/member_board_live.py sierra 2026-06  # one company, one month
"""
import datetime as dt
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from command_center_live import (fetch_all, local_today, _load_json,
                                 map_companies, update_history)
from csr_board_live import EXCLUDE_NAME, month_window_utc, _month_key, _iso_z
from tech_board_live import _is_memb_sku

HISTORY_FILE = os.path.join(ROOT, "data", "member-board-history.json")
MONTH_FREEZE_DAYS = 10
MONTH_RECHECK_HOURS = 24

COMPANIES = {
    "sierra":   {"tenant": "SIE", "tz": "pacific",  "label": "Sierra",   "color": "#1663c7"},
    "ultimate": {"tenant": "ULT", "tz": "mountain", "label": "Ultimate", "color": "#c7161d"},
    "russett":  {"tenant": "RUS", "tz": "arizona",  "label": "Russett",  "color": "#0e7a3d"},
}

# Extended labor warranties live in ST as membership types but are not
# maintenance memberships - keep them out of every count on this board.
NOT_A_MEMBERSHIP = re.compile(r"warranty", re.I)

BILLING_KEYS = ("Monthly", "Annual", "OneTime", "Quarterly", "BiAnnual")


# ---------------------------------------------------------------- reference data
def membership_types(company):
    """{typeId: name} for every membership type, active or not."""
    co = COMPANIES[company]
    types = fetch_all(co["tenant"], "/memberships/v2/tenant/{tenant}/membership-types",
                      {"active": "Any"}, page_size=200, max_pages=5)
    return {t["id"]: t.get("name") or "" for t in types}

def real_type_ids(type_names):
    return {tid for tid, name in type_names.items()
            if not NOT_A_MEMBERSHIP.search(name)}

def _trade_of(bu_name):
    n = (bu_name or "").lower()
    if re.search(r"plumb|drain|sewer|water", n):
        return "plumbing"
    if re.search(r"electric", n):
        return "electrical"
    if re.search(r"hvac|air|heat|cool", n):
        return "hvac"
    return "other"

def business_unit_names(company):
    co = COMPANIES[company]
    bus = fetch_all(co["tenant"], "/settings/v2/tenant/{tenant}/business-units",
                    {}, page_size=200, max_pages=5)
    return {b["id"]: (b.get("name") or "").strip() for b in bus}

def seller_roster(company):
    """Everyone a membership's soldById can point at: {id: {name, kind, sub}}.
    kind is 'tech' or 'office'; sub is the tech's team or the employee's role.
    Includes inactive people so historical sales still resolve to a name."""
    co = COMPANIES[company]
    roster = {}
    for e in fetch_all(co["tenant"], "/settings/v2/tenant/{tenant}/employees",
                       {"active": "Any"}, page_size=200, max_pages=20):
        name = re.sub(r"\s+", " ", (e.get("name") or "").strip())
        if not name:
            continue
        roster[e["id"]] = {"name": name, "kind": "office",
                           "sub": e.get("role") or "",
                           "active": bool(e.get("active", True)),
                           "person": not EXCLUDE_NAME.search(name)}
    for t in fetch_all(co["tenant"], "/settings/v2/tenant/{tenant}/technicians",
                       {"active": "Any"}, page_size=200, max_pages=20):
        name = re.sub(r"\s+", " ", (t.get("name") or "").strip())
        if not name:
            continue
        roster[t["id"]] = {"name": name, "kind": "tech",
                           "sub": t.get("team") or "",
                           "active": bool(t.get("active", True)),
                           "person": not EXCLUDE_NAME.search(name)}
    return roster


# ---------------------------------------------------------------- member spans
def _member_spans(company, customer_ids, real_ids, bu_names):
    """{customerId: [(from_day, end_day_or_None, selling_trade)]} for real
    memberships, batched 100 customers per request."""
    co = COMPANIES[company]
    ids = sorted(customer_ids)
    batches = [ids[i:i + 100] for i in range(0, len(ids), 100)]
    out = {}

    def one(batch):
        return fetch_all(co["tenant"], "/memberships/v2/tenant/{tenant}/memberships",
                         {"customerIds": ",".join(map(str, batch))},
                         page_size=200, max_pages=5)

    with ThreadPoolExecutor(max_workers=4) as ex:
        for rows in ex.map(one, batches):
            for m in rows:
                if m.get("membershipTypeId") not in real_ids:
                    continue
                start = (m.get("from") or m.get("createdOn") or "")[:10]
                if not start:
                    continue
                end = (m.get("to") or "")[:10] or None
                cancel = (m.get("cancellationDate") or "")[:10] or None
                if cancel and (end is None or cancel < end):
                    end = cancel
                trade = _trade_of(bu_names.get(m.get("businessUnitId")))
                out.setdefault(m["customerId"], []).append((start, end, trade))
    return out

def _member_on(spans, cid, day, buffer_days=0):
    """(was a member before `day`, trades their plans were sold in).
    buffer_days requires the membership to have started that many days
    earlier - the Summit deck's revenue chart uses 7, so a membership sold
    with an install doesn't count its own install as member revenue."""
    if buffer_days:
        d = dt.date.fromisoformat(day) - dt.timedelta(days=buffer_days)
        cutoff = d.isoformat()
    else:
        cutoff = day
    trades = set()
    member = False
    for start, end, trade in spans.get(cid, ()):
        if start <= cutoff and start < day and (end is None or end >= day):
            member = True
            trades.add(trade)
    return member, trades


# ---------------------------------------------------------------- window core
def _new_counters():
    return {"sold": 0, "soldBilling": {}, "canceled": 0, "checksWon": 0,
            "checksWonHvac": 0, "checksWonPlumb": 0,
            "jobsCompleted": 0, "sellers": {}, "byBU": {},
            "totalRev": 0.0, "memberRev": 0.0, "crossRev": 0.0,
            "nonMemberJobs": 0, "offered": 0, "techOffer": {}}

def compute_month(company, year, month, real_ids, bu_names):
    """Raw counters for one tenant-local calendar month."""
    co = COMPANIES[company]
    tenant = co["tenant"]
    start, end = month_window_utc(co["tz"], year, month)
    c = _new_counters()

    # SELL - new memberships created in the window
    sold_same_day = set()   # (customerId, created day) -> offer credit on jobs
    for m in fetch_all(tenant, "/memberships/v2/tenant/{tenant}/memberships",
                       {"createdOnOrAfter": start, "createdBefore": end},
                       page_size=500, max_pages=40):
        if m.get("membershipTypeId") not in real_ids:
            continue
        c["sold"] += 1
        bill = m.get("billingFrequency") or "Unknown"
        c["soldBilling"][bill] = c["soldBilling"].get(bill, 0) + 1
        seller = m.get("soldById")
        if seller:
            s = c["sellers"].setdefault(str(seller), {"n": 0, "mo": 0})
            s["n"] += 1
            if bill == "Monthly":
                s["mo"] += 1
        bu = bu_names.get(m.get("businessUnitId"))
        if bu:
            c["byBU"][bu] = c["byBU"].get(bu, 0) + 1
        if m.get("customerId"):
            sold_same_day.add((m["customerId"], (m.get("createdOn") or "")[:10]))

    # RETAIN - cancels whose effective date falls in this month. A cancel
    # touches modifiedOn, so fetch everything modified since month start
    # (cancels are sometimes entered after the effective month closes).
    lo, hi = start[:10], end[:10]
    for m in fetch_all(tenant, "/memberships/v2/tenant/{tenant}/memberships",
                       {"modifiedOnOrAfter": start},
                       page_size=500, max_pages=40):
        if m.get("membershipTypeId") not in real_ids:
            continue
        cancel = (m.get("cancellationDate") or "")[:10]
        if cancel and lo <= cancel < hi:
            c["canceled"] += 1

    # ACTIVATE - system checks completed (event reached Won) in the window,
    # split by trade from the recurring service's name
    for rse in fetch_all(
            tenant, "/memberships/v2/tenant/{tenant}/recurring-service-events",
            {"status": "Won", "modifiedOnOrAfter": start, "modifiedBefore": end},
            page_size=500, max_pages=40):
        c["checksWon"] += 1
        if _trade_of(rse.get("locationRecurringServiceName")) == "plumbing":
            c["checksWonPlumb"] += 1
        else:
            c["checksWonHvac"] += 1

    # Attach-rate denominator - every completed job is a membership chance
    jobs = fetch_all(tenant, "/jpm/v2/tenant/{tenant}/jobs",
                     {"completedOnOrAfter": start, "completedBefore": end},
                     page_size=500, max_pages=40)
    c["jobsCompleted"] = len(jobs)

    # Offer detection - jobs whose estimates carry a membership SKU
    offer_jobs = set()
    for est in fetch_all(tenant, "/sales/v2/tenant/{tenant}/estimates",
                         {"createdOnOrAfter": start, "createdBefore": end},
                         page_size=500, max_pages=40):
        jid = est.get("jobId")
        if jid and any(_is_memb_sku(company, (it.get("sku") or {}).get("name"))
                       for it in (est.get("items") or [])):
            offer_jobs.add(jid)

    # Job -> tech attribution via payroll splits (the tech board's method;
    # splits are created at assignment, so buffer the window start)
    buf = _iso_z(dt.datetime.strptime(start, "%Y-%m-%dT%H:%M:%SZ")
                 - dt.timedelta(days=60))
    splits_by_job = {}
    for s in fetch_all(tenant, "/payroll/v2/tenant/{tenant}/jobs/splits",
                       {"createdOnOrAfter": buf, "createdBefore": end},
                       page_size=500, max_pages=100):
        if (s.get("split") or 0) > 0:
            splits_by_job.setdefault(s["jobId"], set()).add(s["technicianId"])

    # Revenue - invoices dated in the calendar month (invoiceDate is a
    # calendar date stored as midnight UTC, so the window is calendar too)
    inv_start = f"{year:04d}-{month:02d}-01T00:00:00Z"
    inv_end = f"{year + (month == 12):04d}-{month % 12 + 1:02d}-01T00:00:00Z"
    invoices = fetch_all(tenant, "/accounting/v2/tenant/{tenant}/invoices",
                         {"invoicedOnOrAfter": inv_start, "invoicedOnBefore": inv_end},
                         page_size=500, max_pages=60)

    # one membership lookup covers both the jobs and the invoices
    cust_ids = ({j.get("customerId") for j in jobs if j.get("customerId")} |
                {(i.get("customer") or {}).get("id") for i in invoices
                 if (i.get("customer") or {}).get("id")})
    spans = _member_spans(company, cust_ids, real_ids, bu_names)

    for inv in invoices:
        sub = float(inv.get("subTotal") or 0)
        c["totalRev"] += sub
        cid = (inv.get("customer") or {}).get("id")
        day = (inv.get("invoiceDate") or "")[:10]
        if not cid or not day:
            continue
        member, trades = _member_on(spans, cid, day, buffer_days=7)
        if member:
            c["memberRev"] += sub
            inv_trade = _trade_of((inv.get("businessUnit") or {}).get("name"))
            if trades and inv_trade != "other" and inv_trade not in trades:
                c["crossRev"] += sub

    for j in jobs:
        cid = j.get("customerId")
        day = (j.get("completedOn") or "")[:10]
        if not cid or not day:
            continue
        member, _ = _member_on(spans, cid, day)
        if member:
            continue
        c["nonMemberJobs"] += 1
        offered = j.get("id") in offer_jobs or (cid, day) in sold_same_day
        if offered:
            c["offered"] += 1
        for tid in splits_by_job.get(j.get("id"), ()):
            t = c["techOffer"].setdefault(str(tid), {"nm": 0, "off": 0})
            t["nm"] += 1
            t["off"] += offered

    c["totalRev"] = round(c["totalRev"], 2)
    c["memberRev"] = round(c["memberRev"], 2)
    c["crossRev"] = round(c["crossRev"], 2)
    return c


# ---------------------------------------------------------------- snapshot
def compute_snapshot(company, real_ids, type_names):
    """Point-in-time view of the active membership base. Also tallies each
    seller's retention book - the active members they originally sold, the
    count their $5/yr retention bonus is paid on."""
    co = COMPANIES[company]
    billing, by_type, book, total = {}, {}, {}, 0
    for m in fetch_all(co["tenant"], "/memberships/v2/tenant/{tenant}/memberships",
                       {"status": "Active"}, page_size=500, max_pages=100):
        tid = m.get("membershipTypeId")
        if tid not in real_ids:
            continue
        total += 1
        bill = m.get("billingFrequency") or "Unknown"
        billing[bill] = billing.get(bill, 0) + 1
        by_type[tid] = by_type.get(tid, 0) + 1
        seller = m.get("soldById")
        if seller:
            book[seller] = book.get(seller, 0) + 1
    plans = sorted(({"name": type_names.get(tid, "Unknown"), "n": n}
                    for tid, n in by_type.items()), key=lambda p: -p["n"])
    return {"active": total, "billing": billing,
            "pctMonthly": round(billing.get("Monthly", 0) / total * 100, 1) if total else 0,
            "plans": plans[:12], "book": book}


# ---------------------------------------------------------------- caching
def months_of_year(company):
    today = local_today(COMPANIES[company]["tz"])
    return [(today.year, m) for m in range(1, today.month + 1)], today

def compute_company(company, deadline=None, progress=None):
    """Counters for every month this year, cached for closed months.
    Returns (months_dict, roster, snapshot, complete)."""
    cache = _load_json(HISTORY_FILE, {})
    co_cache = cache.get(company, {})
    months, today = months_of_year(company)
    current_key = _month_key(today.year, today.month)
    complete = True

    type_names = membership_types(company)
    real_ids = real_type_ids(type_names)
    bu_names = business_unit_names(company)
    roster = seller_roster(company)
    snapshot = compute_snapshot(company, real_ids, type_names)

    result = {}
    for year, month in months:
        key = _month_key(year, month)
        entry = co_cache.get(key)
        # months cached before the newest metrics existed are recomputed
        if entry and "techOffer" not in entry.get("m", {}):
            entry = None
        if entry and key != current_key:
            month_end = dt.date(year + (month == 12), month % 12 + 1, 1)
            frozen = (today - month_end).days >= MONTH_FREEZE_DAYS and entry.get("final")
            fresh = time.time() - entry.get("at", 0) < MONTH_RECHECK_HOURS * 3600
            if frozen or fresh:
                result[key] = entry["m"]
                continue
        if deadline and time.time() > deadline and key != current_key:
            complete = False   # out of time - keep whatever cache we have
            if entry:
                result[key] = entry["m"]
            continue
        t0 = time.time()
        counters = compute_month(company, year, month, real_ids, bu_names)
        result[key] = counters
        if key != current_key:
            month_end = dt.date(year + (month == 12), month % 12 + 1, 1)
            update_history(HISTORY_FILE, company, key,
                           {"at": time.time(), "m": counters,
                            "final": (today - month_end).days >= MONTH_FREEZE_DAYS})
        if progress:
            progress(company, key, time.time() - t0)
    return result, roster, snapshot, complete


# ---------------------------------------------------------------- public API
def _sum_counters(dicts):
    total = _new_counters()
    for d in dicts:
        for k in ("sold", "canceled", "checksWon", "checksWonHvac", "checksWonPlumb",
                  "jobsCompleted", "totalRev", "memberRev", "crossRev",
                  "nonMemberJobs", "offered"):
            total[k] += d.get(k, 0)
        for tid, t in d.get("techOffer", {}).items():
            agg = total["techOffer"].setdefault(tid, {"nm": 0, "off": 0})
            agg["nm"] += t["nm"]
            agg["off"] += t["off"]
        for k, v in d["soldBilling"].items():
            total["soldBilling"][k] = total["soldBilling"].get(k, 0) + v
        for k, v in d["byBU"].items():
            total["byBU"][k] = total["byBU"].get(k, 0) + v
        for sid, s in d["sellers"].items():
            t = total["sellers"].setdefault(sid, {"n": 0, "mo": 0})
            t["n"] += s["n"]
            t["mo"] += s["mo"]
    return total

def _kpis(c):
    sold, jobs = c["sold"], c["jobsCompleted"]
    monthly = c["soldBilling"].get("Monthly", 0)
    total_rev, member_rev = c.get("totalRev", 0), c.get("memberRev", 0)
    cross_rev = c.get("crossRev", 0)
    nm_jobs, offered = c.get("nonMemberJobs", 0), c.get("offered", 0)
    return {
        "sold": sold,
        "canceled": c["canceled"],
        "net": sold - c["canceled"],
        "checksWon": c["checksWon"],
        "checksWonHvac": c.get("checksWonHvac", 0),
        "checksWonPlumb": c.get("checksWonPlumb", 0),
        "jobsCompleted": jobs,
        "attachRate": round(sold / jobs * 100, 1) if jobs else 0,
        "pctSoldMonthly": round(monthly / sold * 100, 1) if sold else 0,
        "soldBilling": c["soldBilling"],
        "totalRev": round(total_rev, 2),
        "memberRev": round(member_rev, 2),
        "pctMemberRev": round(member_rev / total_rev * 100, 1) if total_rev else 0,
        "crossRev": round(cross_rev, 2),
        "pctCrossRev": round(cross_rev / member_rev * 100, 1) if member_rev else 0,
        "nonMemberJobs": nm_jobs,
        "offered": offered,
        "offerRate": round(offered / nm_jobs * 100, 1) if nm_jobs else 0,
        "missedOffers": nm_jobs - offered,
    }

def _seller_rows(c, roster, company):
    co = COMPANIES[company]
    tech_offer = c.get("techOffer", {})
    techs, csrs = [], []
    for sid, s in c["sellers"].items():
        info = roster.get(int(sid))
        if info and not info["person"]:
            continue   # answering service / system accounts
        row = {"id": int(sid),
               "name": info["name"] if info else "Unknown",
               "sub": info["sub"] if info else "",
               "company": company, "companyLabel": co["label"], "color": co["color"],
               "sold": s["n"], "soldMonthly": s["mo"],
               "pctMonthly": round(s["mo"] / s["n"] * 100) if s["n"] else 0}
        if info and info["kind"] == "tech":
            t = tech_offer.get(sid, {"nm": 0, "off": 0})
            row["nmJobs"] = t["nm"]
            row["offerRate"] = round(t["off"] / t["nm"] * 100) if t["nm"] else 0
        (techs if info and info["kind"] == "tech" else csrs).append(row)
    key = lambda r: (-r["sold"], -r["soldMonthly"], r["name"])
    return sorted(techs, key=key), sorted(csrs, key=key)

def _book_rows(book, roster, company):
    """Retention-book leaderboards: active members still on the books per
    seller, restricted to people currently employed (bonus-eligible)."""
    co = COMPANIES[company]
    techs, csrs = [], []
    for sid, n in book.items():
        info = roster.get(sid)
        if not info or not info["person"] or not info["active"]:
            continue
        row = {"id": sid, "name": info["name"], "sub": info["sub"],
               "company": company, "companyLabel": co["label"], "color": co["color"],
               "book": n, "bonusYr": 5 * n}
        (techs if info["kind"] == "tech" else csrs).append(row)
    key = lambda r: (-r["book"], r["name"])
    return sorted(techs, key=key), sorted(csrs, key=key)

def _bu_rows(c):
    return sorted(({"name": k, "n": v} for k, v in c["byBU"].items()),
                  key=lambda b: -b["n"])[:12]

def compute(time_budget_secs=None, progress=None):
    """Full data.json payload for the dashboard."""
    deadline = time.time() + time_budget_secs if time_budget_secs else None
    complete = True

    results = map_companies(
        lambda co: compute_company(co, deadline=deadline, progress=progress),
        COMPANIES)

    boards = {"mtd": {}, "ytd": {}}
    snapshot, trend, book = {}, {}, {}
    for company in COMPANIES:
        per_month, roster, snap, ok = results[company]
        complete = complete and ok
        _, today = months_of_year(company)
        current_key = _month_key(today.year, today.month)
        b_techs, b_csrs = _book_rows(snap.pop("book", {}), roster, company)
        book[company] = {"techs": b_techs, "csrs": b_csrs}
        snapshot[company] = snap
        trend[company] = [{"m": k, "sold": c["sold"], "canceled": c["canceled"],
                           "checksWon": c["checksWon"]}
                          for k, c in sorted(per_month.items())]
        for view, counters in (("mtd", per_month.get(current_key, _new_counters())),
                               ("ytd", _sum_counters(per_month.values()))):
            techs, csrs = _seller_rows(counters, roster, company)
            boards[view][company] = {"kpis": _kpis(counters), "techs": techs,
                                     "csrs": csrs, "byBU": _bu_rows(counters)}

    # combined = sum of the three companies
    for view in boards:
        per_co = [boards[view][c] for c in COMPANIES]
        sum_keys = ("sold", "canceled", "net", "checksWon", "checksWonHvac",
                    "checksWonPlumb", "jobsCompleted",
                    "totalRev", "memberRev", "crossRev", "nonMemberJobs", "offered")
        kpis = {k: 0 for k in sum_keys}
        kpis["soldBilling"] = {}
        for b in per_co:
            for k in sum_keys:
                kpis[k] += b["kpis"][k]
            for k, v in b["kpis"]["soldBilling"].items():
                kpis["soldBilling"][k] = kpis["soldBilling"].get(k, 0) + v
        for k in ("totalRev", "memberRev", "crossRev"):
            kpis[k] = round(kpis[k], 2)
        kpis["attachRate"] = (round(kpis["sold"] / kpis["jobsCompleted"] * 100, 1)
                              if kpis["jobsCompleted"] else 0)
        kpis["pctSoldMonthly"] = (round(kpis["soldBilling"].get("Monthly", 0)
                                        / kpis["sold"] * 100, 1) if kpis["sold"] else 0)
        kpis["pctMemberRev"] = (round(kpis["memberRev"] / kpis["totalRev"] * 100, 1)
                                if kpis["totalRev"] else 0)
        kpis["pctCrossRev"] = (round(kpis["crossRev"] / kpis["memberRev"] * 100, 1)
                               if kpis["memberRev"] else 0)
        kpis["offerRate"] = (round(kpis["offered"] / kpis["nonMemberJobs"] * 100, 1)
                             if kpis["nonMemberJobs"] else 0)
        kpis["missedOffers"] = kpis["nonMemberJobs"] - kpis["offered"]
        key = lambda r: (-r["sold"], -r["soldMonthly"], r["name"])
        boards[view]["combined"] = {
            "kpis": kpis,
            "techs": sorted((r for c in COMPANIES for r in boards[view][c]["techs"]), key=key),
            "csrs": sorted((r for c in COMPANIES for r in boards[view][c]["csrs"]), key=key),
            "byBU": [],
        }

    key = lambda r: (-r["book"], r["name"])
    book["combined"] = {
        "techs": sorted((r for c in COMPANIES for r in book[c]["techs"]), key=key),
        "csrs": sorted((r for c in COMPANIES for r in book[c]["csrs"]), key=key),
    }

    snapshot["combined"] = {
        "active": sum(snapshot[c]["active"] for c in COMPANIES),
        "billing": {k: sum(snapshot[c]["billing"].get(k, 0) for c in COMPANIES)
                    for k in BILLING_KEYS},
        "plans": [],
    }
    tot = snapshot["combined"]["active"]
    snapshot["combined"]["pctMonthly"] = (
        round(snapshot["combined"]["billing"].get("Monthly", 0) / tot * 100, 1) if tot else 0)

    trend["combined"] = []
    keys = sorted({row["m"] for c in COMPANIES for row in trend[c]})
    for k in keys:
        agg = {"m": k, "sold": 0, "canceled": 0, "checksWon": 0}
        for c in COMPANIES:
            for row in trend[c]:
                if row["m"] == k:
                    for f in ("sold", "canceled", "checksWon"):
                        agg[f] += row[f]
        trend["combined"].append(agg)

    today = local_today("pacific")
    return {
        "updated": dt.datetime.now().strftime("%a %b %d %Y %H:%M:%S"),
        "complete": complete,
        "period": {"mtd": today.strftime("%B %Y"), "ytd": str(today.year)},
        "companies": {c: {"label": co["label"], "color": co["color"]}
                      for c, co in COMPANIES.items()},
        "snapshot": snapshot,
        "boards": boards,
        "book": book,
        "trend": trend,
    }


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    import json as _json
    if len(sys.argv) > 1:
        company = sys.argv[1]
        type_names = membership_types(company)
        real_ids = real_type_ids(type_names)
        bu_names = business_unit_names(company)
        if len(sys.argv) > 2:
            y, m = map(int, sys.argv[2].split("-"))
        else:
            t = local_today(COMPANIES[company]["tz"])
            y, m = t.year, t.month
        c = compute_month(company, y, m, real_ids, bu_names)
        c["sellers"] = dict(sorted(c["sellers"].items(),
                                   key=lambda kv: -kv[1]["n"])[:10])
        print(_json.dumps(c, indent=1))
    else:
        data = compute()
        for co in data["boards"]["mtd"]:
            b = data["boards"]["mtd"][co]
            print(co, b["kpis"], "| top tech:", b["techs"][:1], "| top csr:", b["csrs"][:1])
