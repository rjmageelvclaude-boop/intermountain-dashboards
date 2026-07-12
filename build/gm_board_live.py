#!/usr/bin/env python3
"""
Live ServiceTitan engine for the HVAC Install Gross Margin board.

Automates the hand-made monthly "GROSS MARGIN REPORT" workbook (Sierra,
business unit 337 "HVAC - Install - AOR"): every install job completed in the
month with its revenue, discounts, crew performance pay, payroll adjustments,
PO costs, returns, Costco fee and financing fee, plus a month summary that is
reconciled to the accounting P&L.

Where each column comes from (validated against the June 2025 hand report -
all 145 jobs matched, every column within a few % - and against the SLLC
June 2026 financial statements - net revenue within 0.7%):

  jobs            jpm/v2 jobs completed in the month, businessUnitId 337,
                  total > 0 (zero-total warranty/QA/drywall jobs excluded,
                  same population as the hand report)
  revenue         job.total (net of on-invoice discounts)
  list price      sum of the job's positive invoice items - the pre-discount
                  price ("Pricebook Price" column of the hand report)
  discounts       invoice items of type PriceModifier (negative)
  costco fee      the $0 adjustment invoice ServiceTitan books on Costco jobs:
                  a Material item named "Costco Fees - 6020" whose totalCost
                  is the fee owed to Costco
  perf pay        payroll gross-pay-items of type InvoiceRelatedBonus linked
                  to the job (piece work + sales commissions), except:
  payroll adj     ...items with activity "Direct Adjustment", kept separate
                  like the hand report's Payroll Adjustments column
  actual costs    non-canceled purchase orders on the job (equipment,
                  materials, permits, subs)
  returns         inventory returns on the job (credit, shown negative)
  fin fee         4% dealer fee (the hand report's convention) on payments of
                  the financing payment types applied to the job's invoices
  costco rebate   payments of type "Costco Retail Discounts" - the member
                  cash-card portion the books treat as contra revenue (42035)

Report GM per job keeps the hand report's exact formula, including its flat
$350/job overhead adder:

  GM% = (total - (perf + padj + cost + costco + fin + ret + 350)) / total

Month summary adds the accounting view:
  net revenue   = list + discounts - costco rebate - fin fee   (P&L: total
                  revenue net of 42030/42035/42050 contra accounts)
  job COGS      = perf + padj + cost + ret + costco fee
  est. book GM  = job GM minus OVERHEAD_RATE - the COGS the books carry that
                  is not job-costed (payroll taxes, contract labor, warranty
                  labor, delivery, shop). 5.5% of net revenue, calibrated so
                  June 2026 matches the financial statements' 48.3%.

Closed months are cached in data/gm-board-history.json and recomputed at most
daily until 45 days past month-end (Costco fee invoices and payroll
adjustments trickle in for weeks), then frozen. The current month is
recomputed on every run.

CLI smoke test:
    py build/gm_board_live.py               # current month, summary print
    py build/gm_board_live.py 2026-06       # one month
"""
import base64
import datetime as dt
import json
import os
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from command_center_live import (fetch_all, local_today, _load_json,
                                 _utc_offset_hours, update_history)
from servicetitan_client import st_get
from tech_board_live import month_window_utc, clean_name

HISTORY_FILE = os.path.join(ROOT, "data", "gm-board-history.json")

TENANT, TZ = "SIE", "pacific"
BUSINESS_UNIT = 337              # HVAC - Install - AOR
FIRST_MONTH = (2026, 1)          # YTD scope, same as the financial statements
FLAT_OVERHEAD = 350.0            # the hand report's per-job overhead adder
OVERHEAD_RATE = 0.055            # non-job-costed COGS (payroll taxes, contract
                                 # + warranty labor, shop, delivery) as % of net
                                 # revenue; calibrated: June 2026 books = 48.3%
FIN_FEE_RATE = 0.04              # dealer fee on financed jobs (hand report's 4%)
FIN_TYPES = {307999122, 328421261,   # Service Finance / Service Finance2
             308002190, 323467676,   # GoodLeap / GoodLeap2
             572182335, 600810871}   # Turns Financing / Turns Financing Fees
COSTCO_REBATE_TYPE = 566675212   # "Costco Retail Discounts" (books: 42035)

# Customer privacy: the repo and Pages site are public, and the repo rule is
# "never commit customer names". Rows ship without the customer column; the
# {jobId: customer} map ships only as an AES-256-GCM blob under this key
# (env GM_BOARD_KEY / GitHub secret, or secrets/gm-board-key.txt locally).
# The page derives the decryption key from the access code the viewer already
# typed, so set the secret to the same value as the board access code.
# No key -> data.json simply has no customer names.
GM_KEY_ENV = "GM_BOARD_KEY"
GM_KEY_FILE = os.path.join(ROOT, "secrets", "gm-board-key.txt")
KDF_ITERATIONS = 310_000
AAD = b"hyperion-gm-board-v1"

MONTH_FREEZE_DAYS = 45           # fees/pay adjustments trickle in for weeks
MONTH_RECHECK_HOURS = 24
MAX_BACKFILL_MONTHS = 2          # stale closed months recomputed per run
PAY_WINDOW_BEFORE = 75           # commissions can be paid at sale, well before
PAY_WINDOW_AFTER = 75            # ...or adjusted well after completion
PO_WINDOW_BEFORE = 180           # POs are cut when the job is created

COMPANY = {"label": "Sierra", "color": "#1663c7"}


# ---------------------------------------------------------------- name maps
def _people_names():
    """{id: name} across employees + technicians (active and not)."""
    names = {}
    for path in ("/settings/v2/tenant/{tenant}/employees",
                 "/settings/v2/tenant/{tenant}/technicians"):
        for p in fetch_all(TENANT, path, {"active": "Any"}, page_size=200,
                           max_pages=40):
            names.setdefault(p["id"], clean_name(p.get("name")))
    return names


def _customer_names(customer_ids):
    """{id: name} fetched in batches of 50."""
    ids = sorted(set(customer_ids))
    names = {}
    def batch(chunk):
        r = st_get(TENANT, "/crm/v2/tenant/{tenant}/customers",
                   params={"ids": ",".join(map(str, chunk)), "pageSize": 100})
        return r.get("data") or []
    chunks = [ids[i:i + 50] for i in range(0, len(ids), 50)]
    with ThreadPoolExecutor(6) as ex:
        for rows in ex.map(batch, chunks):
            for c in rows:
                names[c["id"]] = (c.get("name") or "").strip()
    return names


def _payment_type_names():
    r = st_get(TENANT, "/accounting/v2/tenant/{tenant}/payment-types",
               params={"pageSize": 200})
    return {int(p["id"]): (p.get("name") or "").strip() for p in (r.get("data") or [])}


# ---------------------------------------------------------------- shared pulls
# Several list endpoints here (gross-pay-items above all) return rows in a
# NONDETERMINISTIC order, so offset pagination silently skips and duplicates
# rows - two identical wide pulls returned the same count but different items,
# moving June performance pay by $85k. The fix: fetch in date slices small
# enough that a slice fits in ONE 5000-row page (no pagination, one request =
# one consistent snapshot), and dedupe by id where ids exist. The date params
# behave as [from, to): "dateOnOrBefore"/"...Before" are exclusive of the end
# date (verified: a same-day [d, d] window returns zero rows).

def _sliced(path, p_from, p_to, day_from, day_to, slice_days, id_field=None,
            overlap_days=0):
    seen, out, cur = set(), [], day_from
    while cur <= day_to:
        end = min(cur + dt.timedelta(days=slice_days - 1), day_to)
        params = {p_from: (cur - dt.timedelta(days=overlap_days)).isoformat(),
                  p_to: (end + dt.timedelta(days=1)).isoformat()}
        page = 1
        while True:   # hasMore is rare at pageSize 5000; tail risk accepted
            r = st_get(TENANT, path, params=dict(params, pageSize=5000, page=page))
            rows = r.get("data") or []
            if id_field:
                for row in rows:
                    if row[id_field] not in seen:
                        seen.add(row[id_field])
                        out.append(row)
            else:
                out.extend(rows)
            if not r.get("hasMore"):
                break
            page += 1
        cur = end + dt.timedelta(days=1)
    return out


def shared_data(earliest, progress=None):
    """One pull per dataset covering every month recomputed this run.
    Attribution to a month is by jobId / invoiceId, so a wide date window can
    only add coverage, never bleed across months."""
    say = progress or (lambda *_: None)
    today = local_today(TZ)

    # gross-pay-items have NO id -> disjoint 2-day slices, no dedupe possible
    pay_from = (dt.date(*earliest, 1) - dt.timedelta(days=PAY_WINDOW_BEFORE))
    gpi = _sliced("/payroll/v2/tenant/{tenant}/gross-pay-items",
                  "dateOnOrAfter", "dateOnOrBefore", pay_from, today,
                  slice_days=2)
    say("gpi", len(gpi))

    po_from = (dt.date(*earliest, 1) - dt.timedelta(days=PO_WINDOW_BEFORE))
    pos = _sliced("/inventory/v2/tenant/{tenant}/purchase-orders",
                  "createdOnOrAfter", "createdBefore", po_from, today,
                  slice_days=14, id_field="id", overlap_days=1)
    say("pos", len(pos))

    rets = _sliced("/inventory/v2/tenant/{tenant}/returns",
                   "createdOnOrAfter", "createdBefore", po_from, today,
                   slice_days=60, id_field="id", overlap_days=1)
    say("returns", len(rets))

    pays = _sliced("/accounting/v2/tenant/{tenant}/payments",
                   "paidOnAfter", "paidOnBefore",
                   dt.date(*earliest, 1) - dt.timedelta(days=60), today,
                   slice_days=7, id_field="id", overlap_days=1)
    say("payments", len(pays))

    jts = fetch_all(TENANT, "/jpm/v2/tenant/{tenant}/job-types", {},
                    page_size=200, max_pages=20)
    return {
        "gpi": gpi, "pos": pos, "rets": rets, "pays": pays,
        "jobTypes": {t["id"]: t["name"] for t in jts},
        "people": _people_names(),
        "payTypes": _payment_type_names(),
    }


# ---------------------------------------------------------------- month core
def _segment(type_name):
    t = type_name.lower()
    if "costco" in t:
        return "Costco"
    if "tgl" in t:
        return "TGL"
    if "lto" in t:
        return "LTO"
    return "Direct"


def _job_invoices(job_ids):
    """{jobId: [invoice, ...]} - primary + adjustment invoices (Costco fees
    ride on $0 adjustment invoices, so every invoice matters)."""
    def one(jid):
        r = st_get(TENANT, "/accounting/v2/tenant/{tenant}/invoices",
                   params={"jobId": jid, "pageSize": 100})
        return jid, (r.get("data") or [])
    out = {}
    with ThreadPoolExecutor(8) as ex:
        for jid, invs in ex.map(one, job_ids):
            out[jid] = invs
    return out


def compute_month(year, month, shared):
    """(rows, summary) for one calendar month."""
    start, end = month_window_utc(TZ, year, month)
    jobs = fetch_all(TENANT, "/jpm/v2/tenant/{tenant}/jobs",
                     {"completedOnOrAfter": start, "completedBefore": end,
                      "jobStatus": "Completed"}, page_size=500, max_pages=100)
    install = [j for j in jobs if j.get("businessUnitId") == BUSINESS_UNIT
               and float(j.get("total") or 0) > 0]
    ids = {j["id"] for j in install}

    invs_by_job = _job_invoices(ids)
    inv_to_job = {int(inv["id"]): jid
                  for jid, invs in invs_by_job.items() for inv in invs}

    perf, padj = defaultdict(float), defaultdict(float)
    for i in shared["gpi"]:
        jid = i.get("jobId")
        if jid not in ids or i.get("grossPayItemType") != "InvoiceRelatedBonus":
            continue
        amt = float(i.get("amount") or 0) + float(i.get("amountAdjustment") or 0)
        if i.get("activity") == "Direct Adjustment":
            padj[jid] += amt
        else:
            perf[jid] += amt

    po_cost = defaultdict(float)
    for p in shared["pos"]:
        if p.get("jobId") in ids and p.get("status") != "Canceled":
            po_cost[p["jobId"]] += float(p.get("total") or 0)

    ret_credit = defaultdict(float)
    for r in shared["rets"]:
        if r.get("jobId") in ids and not r.get("dateCanceled"):
            ret_credit[r["jobId"]] += float(r.get("returnAmount") or 0)

    financed = defaultdict(float)
    rebate = defaultdict(float)
    pay_types = defaultdict(set)
    for p in shared["pays"]:
        t = int(p.get("typeId") or 0)     # API returns typeId as a string
        for a in (p.get("appliedTo") or []):
            jid = inv_to_job.get(int(a.get("appliedTo") or 0))
            if jid is None:
                continue
            amt = float(a.get("appliedAmount") or 0)
            if t in FIN_TYPES:
                financed[jid] += amt
            elif t == COSTCO_REBATE_TYPE:
                rebate[jid] += amt
            if amt > 0:
                pay_types[jid].add(shared["payTypes"].get(t, "Other"))

    cust = _customer_names(j["customerId"] for j in install)
    tname, people = shared["jobTypes"], shared["people"]

    rows = []
    for j in sorted(install, key=lambda x: x["completedOn"]):
        jid = j["id"]
        total = float(j["total"])
        list_price = disc = costco = 0.0
        for inv in invs_by_job.get(jid, []):
            for it in (inv.get("items") or []):
                t = float(it.get("total") or 0)
                if "costco fee" in (it.get("skuName") or "").lower():
                    costco += float(it.get("totalCost") or 0)
                elif it.get("type") == "PriceModifier":
                    disc += t
                elif t > 0:
                    list_price += t
        fin = round(financed.get(jid, 0) * FIN_FEE_RATE, 2)
        ret = -ret_credit.get(jid, 0)          # credit, negative like the report
        typ = tname.get(j["jobTypeId"], "?")
        costs = (perf.get(jid, 0) + padj.get(jid, 0) + po_cost.get(jid, 0)
                 + costco + fin + ret + FLAT_OVERHEAD)
        lead = (j.get("jobGeneratedLeadSource") or {}).get("employeeId")
        done_utc = dt.datetime.strptime(j["completedOn"][:19], "%Y-%m-%dT%H:%M:%S")
        done_local = (done_utc + dt.timedelta(
            hours=_utc_offset_hours(TZ, done_utc.date()))).date()
        rows.append({
            "id": jid, "d": done_local.isoformat(), "type": typ,
            "seg": _segment(typ),
            "cust": cust.get(j["customerId"], ""),
            "sold": people.get(j.get("soldById"), ""),
            "lead": people.get(lead, ""),
            "total": round(total, 2), "list": round(list_price, 2),
            "disc": round(disc, 2), "perf": round(perf.get(jid, 0), 2),
            "padj": round(padj.get(jid, 0), 2),
            "cost": round(po_cost.get(jid, 0), 2), "ret": round(ret, 2),
            "costco": round(costco, 2), "fin": fin,
            "rebate": round(rebate.get(jid, 0), 2),
            "pay": ", ".join(sorted(pay_types.get(jid, ()))),
            "gm": round((total - costs) / total, 4),
        })
    return rows, summarize(rows, year, month)


def summarize(rows, year, month):
    n = len(rows)
    S = lambda k: sum(r[k] for r in rows)
    total, disc, fin, rebate = S("total"), S("disc"), S("fin"), S("rebate")
    cogs = S("perf") + S("padj") + S("cost") + S("ret") + S("costco")
    report_costs = cogs + fin + FLAT_OVERHEAD * n
    net = S("list") + disc - rebate - fin
    gp = net - cogs
    est_book = (gp - OVERHEAD_RATE * net) / net if net else 0

    # cumulative daily curve for the trend view
    daily, cum_rev, cum_costs = [], 0.0, 0.0
    by_day = defaultdict(list)
    for r in rows:
        by_day[r["d"]].append(r)
    for d in sorted(by_day):
        for r in by_day[d]:
            cum_rev += r["total"]
            cum_costs += (r["perf"] + r["padj"] + r["cost"] + r["ret"]
                          + r["costco"] + r["fin"] + FLAT_OVERHEAD)
        daily.append({"d": d, "rev": round(cum_rev, 2),
                      "gm": round((cum_rev - cum_costs) / cum_rev, 4) if cum_rev else 0})

    def rollup(key, top=None):
        groups = defaultdict(list)
        for r in rows:
            groups[r[key] or "—"].append(r)
        out = []
        for name, rs in groups.items():
            t = sum(r["total"] for r in rs)
            c = sum(r["perf"] + r["padj"] + r["cost"] + r["ret"] + r["costco"]
                    + r["fin"] + FLAT_OVERHEAD for r in rs)
            out.append({"name": name, "n": len(rs), "total": round(t, 2),
                        "gm": round((t - c) / t, 4) if t else 0})
        out.sort(key=lambda g: -g["total"])
        return out[:top] if top else out

    return {
        "n": n, "total": round(total, 2), "list": round(S("list"), 2),
        "disc": round(disc, 2), "perf": round(S("perf"), 2),
        "padj": round(S("padj"), 2), "cost": round(S("cost"), 2),
        "ret": round(S("ret"), 2), "costco": round(S("costco"), 2),
        "fin": round(fin, 2), "rebate": round(rebate, 2),
        "avgTicket": round(total / n, 2) if n else 0,
        "reportGm": round((total - report_costs) / total, 4) if total else 0,
        "netRev": round(net, 2), "jobCogs": round(cogs, 2),
        "jobGp": round(gp, 2),
        "jobGmPct": round(gp / net, 4) if net else 0,
        "estBookGm": round(est_book, 4),
        "daily": daily,
        "segs": rollup("seg"),
        "types": rollup("type", top=12),
        "soldBy": rollup("sold", top=15),
    }


# ---------------------------------------------------------------- encryption
def _gm_key():
    key = os.environ.get(GM_KEY_ENV, "").strip()
    if not key and os.path.exists(GM_KEY_FILE):
        with open(GM_KEY_FILE, encoding="utf-8") as f:
            key = f.read().strip()
    return key or None


def encrypt_blob(payload, passphrase):
    """AES-256-GCM blob the page decrypts via Web Crypto (PBKDF2-SHA256 key
    derivation) - same scheme as the scorecard's pay blob. Returns None if
    the cryptography package is unavailable."""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
        from cryptography.hazmat.primitives import hashes
    except ImportError:
        return None
    salt, nonce = os.urandom(16), os.urandom(12)
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                     iterations=KDF_ITERATIONS).derive(passphrase.encode())
    ct = AESGCM(key).encrypt(nonce, json.dumps(payload).encode(), AAD)
    b64 = lambda b: base64.b64encode(b).decode()
    return {"v": 1, "kdf": "PBKDF2-SHA256", "iter": KDF_ITERATIONS,
            "salt": b64(salt), "nonce": b64(nonce), "ct": b64(ct),
            "aad": AAD.decode()}


# ---------------------------------------------------------------- caching
def _month_key(y, m):
    return f"{y:04d}-{m:02d}"


def months_of_scope():
    today = local_today(TZ)
    y, m = FIRST_MONTH
    months = []
    while (y, m) <= (today.year, today.month):
        months.append((y, m))
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return months, today


def compute(time_budget_secs=None, progress=None):
    """Full data.json payload."""
    deadline = time.time() + time_budget_secs if time_budget_secs else None
    say = progress or (lambda *a: None)
    cache = _load_json(HISTORY_FILE, {})
    co_cache = cache.setdefault("sierra", {})
    months, today = months_of_scope()
    current_key = _month_key(today.year, today.month)

    # which months need recomputing this run
    todo = []
    for (y, m) in months:
        key = _month_key(y, m)
        if key == current_key:
            todo.append((y, m))
            continue
        entry = co_cache.get(key)
        if entry:
            month_end = dt.date(y + (m == 12), m % 12 + 1, 1)
            frozen = (today - month_end).days >= MONTH_FREEZE_DAYS and entry.get("final")
            fresh = time.time() - entry.get("at", 0) < MONTH_RECHECK_HOURS * 3600
            if frozen or fresh:
                continue
        todo.append((y, m))
    # current month always first, then oldest stale months, capped per run
    todo.sort(key=lambda ym: (_month_key(*ym) != current_key, ym))
    deferred = todo[1 + MAX_BACKFILL_MONTHS:]
    todo = todo[:1 + MAX_BACKFILL_MONTHS]

    shared = shared_data(min(todo), progress=lambda w, n: say(f"shared {w}", n))

    complete = not deferred
    result = {}
    for (y, m) in todo:
        key = _month_key(y, m)
        if deadline and time.time() > deadline and key != current_key:
            complete = False
            continue
        t0 = time.time()
        rows, summary = compute_month(y, m, shared)
        result[key] = {"jobs": rows, "summary": summary}
        if key != current_key:
            month_end = dt.date(y + (m == 12), m % 12 + 1, 1)
            update_history(HISTORY_FILE, "sierra", key, {
                "at": time.time(), "jobs": rows, "summary": summary,
                "final": (today - month_end).days >= MONTH_FREEZE_DAYS})
        say(key, time.time() - t0)

    # Customer names never ship in plaintext (public repo + public Pages) -
    # rows go out without "cust"; the {jobId: customer} map goes out only as
    # an encrypted blob, and only when a key is configured.
    key_pass = _gm_key()
    out_months = []
    for (y, m) in months:
        key = _month_key(y, m)
        if key in result:
            entry = result[key]
        elif key in co_cache:
            entry = co_cache[key]
        else:
            complete = False
            continue
        rows = entry["jobs"]
        month_out = {
            "key": key,
            "label": dt.date(y, m, 1).strftime("%B %Y"),
            "summary": entry["summary"],
            "jobs": [{k: v for k, v in r.items() if k != "cust"} for r in rows],
        }
        if key_pass:
            blob = encrypt_blob({str(r["id"]): r.get("cust", "") for r in rows},
                                key_pass)
            if blob:
                month_out["custEnc"] = blob
        out_months.append(month_out)

    return {
        "updated": dt.datetime.now().strftime("%a %b %d %Y %H:%M:%S"),
        "complete": complete,
        "company": COMPANY,
        "current": current_key,
        "flatOverhead": FLAT_OVERHEAD,
        "overheadRate": OVERHEAD_RATE,
        "finFeeRate": FIN_FEE_RATE,
        "months": out_months,
    }


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) > 1:
        y, m = map(int, sys.argv[1].split("-"))
        shared = shared_data((y, m), progress=lambda w, n: print(f"  {w}: {n}"))
        rows, s = compute_month(y, m, shared)
        print(f"\n{_month_key(y, m)}  jobs {s['n']}  revenue {s['total']:,.0f}  "
              f"avg {s['avgTicket']:,.0f}")
        print(f"report GM {s['reportGm']*100:.1f}%   net rev {s['netRev']:,.0f}   "
              f"job GM {s['jobGmPct']*100:.1f}%   est book GM {s['estBookGm']*100:.1f}%")
        for g in s["segs"]:
            print(f"  {g['name']:8s} n={g['n']:<4d} {g['total']:>12,.0f}  GM {g['gm']*100:.1f}%")
        for r in rows[:5]:
            print(f"  {r['d']} {r['type'][:34]:34s} {r['total']:>10,.0f} "
                  f"gm {r['gm']*100:5.1f}%  {r['cust'][:28]}")
    else:
        data = compute(progress=lambda k, v: print(f"  {k}: {v}", flush=True))
        print(json.dumps(data, indent=1)[:1500])
