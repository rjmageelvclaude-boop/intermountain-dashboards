#!/usr/bin/env python3
"""Week-over-Week scoreboard engine - the automated replacement for RJ's
manual Monday PowerPoint (Sierra_WoW_Report_WkN.pptx).

One record per company per week (Mon-Sun, tenant-local; Week 1 is the deck's
3-day stub Fri 5/1 - Sun 5/3/2026). Definitions were reverse-validated
against the deck's Week-10 numbers (2026-07-05 snapshot):

  sales panel      estimates sold in the week (subtotal, Dismissed excluded),
                   split by business-unit bucket (HVAC Sales / HVAC S&M /
                   Plumbing service+maint), plus month-to-date through the
                   week's Sunday - matched the deck's June close to the
                   dollar - and the deck's weekday pace
                   (mtd * weekdaysInMonth / weekdaysElapsed)
  call center      inbound leadCalls deduped by leadCall id, rate =
                   Booked / (Booked+Unbooked+NotBooked); the ST rate excludes
                   AVOCA-answered calls, which get their own rate (note:
                   Avoca's own platform reports a different number - this is
                   how the calls landed in ServiceTitan)
  canceled jobs    distinct jobs with an appointment in the week whose
                   jobStatus is Canceled, bucketed S&M / Sales / Plumbing /
                   Install (canceled estimates = the Sales bucket)
  board capacity   OPPORTUNITIES RAN: completed jobs in the trade's buckets
                   passing the call-board opportunity rule (noCharge /
                   soldThreshold). The deck divides by plan-per-day x 5
                   weekdays - W10 gave 552 vs the deck's 553 (HVAC) and
                   exactly 168 (plumbing)
  ROPPs            completed HVAC-service jobs carrying the ROPP tag net of
                   Management Removed (W10: 166 vs deck 164); removed = the
                   ROPP+Removed overlap (33 vs deck 35). Also the deck's
                   "ROPPs vs plan" numerator and the silo TGL-rate denominator
  HVAC S&M         completed HVAC-service-bucket jobs: opportunity /
                   conversion via soldThreshold; avg ticket = week's S&M
                   SALES / opps (the deck's exact W10 quotient); membership
                   conversion = non-member jobs with a membership sold (SAM /
                   Shield / MVP skus) over non-member OPPORTUNITY jobs
  HVAC silo        TGLs = estimate-TGL-typed jobs created in the week, any
                   generating tech, later-canceled included; same-day /
                   next-day = first-appointment start vs creation day
  HVAC sales (CA)  completed HVAC-Sales-bucket jobs: Costco = the Costco BU
                   (regardless of job type), TGL = TGL-typed remainder,
                   Marketed = the rest. ULTIMATE doesn't type its sales jobs
                   reliably, so there the lead source decides instead: a job
                   with a generated lead (jobGeneratedLeadSource - a tech
                   turned it over) is TGL, no lead source = Marketed.
                   closed = a sold (non-dismissed)
                   estimate on the job in the week. Sold runs get auto-marked
                   No Charge/Non-Opportunity when their project is created,
                   so a run counts when it passes the opportunity rule OR
                   anything was ever sold on it (estimates checked directly
                   for candidates, since a sale can land in another week);
                   No Charge/Non-Opp runs with nothing sold are bad leads
                   and don't count
  plumbing         completed Drains/Service/Maintenance-bucket jobs (install
                   BU excluded per RJ's report filter); units = tankless /
                   tanked / filtration pricebook SKUs on sold estimates tied
                   to any plumbing-bucket job incl. install

Counts drift after a week closes (jobs get canceled/reactivated, estimates
adjusted), which is why a live recompute of an old week lands within a few
percent of the deck's snapshot. Closed weeks freeze FREEZE_DAYS after their
Sunday and never move again.

History lives in data/wow-board-history.json keyed by the week's Monday
(YYYY-MM-DD, 10 chars - safely ignored by update_history's 7-char month
pruning). The in-progress week is recomputed every run and never cached;
closed weeks refresh at most daily until frozen. Backfill is budgeted and
resumable like the other boards.
"""

import datetime as dt
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from command_center_live import (COMPANIES as CC_COMPANIES, HVAC_SERVICE,
                                 PLUMB_ALL, PLUMB_SERVICE, fetch_all,
                                 local_today, _load_json, job_type_names,
                                 map_companies, update_history)
from tech_board_live import (DEFAULT_SOLD_THRESHOLD, _local_completed_day,
                             _member_before, _memberships_for_customers,
                             sold_thresholds, window_utc)
from tech_board_live import _is_memb_sku as hvac_memb_sku
from silo_board_live import _local_day, estimate_job_type_ids
from plumb_board_live import _is_memb_sku as plumb_memb_sku
from csr_board_live import LEAD_TYPES
from call_board_live import is_opportunity

HISTORY_FILE = os.path.join(ROOT, "data", "wow-board-history.json")
# Bump when metric definitions change: cached weeks computed under an older
# version are recomputed instead of served (the Actions cache would otherwise
# keep serving frozen weeks built with the old definitions forever).
DEFS_VER = 4
WEEK1_FROM = dt.date(2026, 5, 1)     # the deck's Week 1: Fri 5/1 - Sun 5/3
WEEK1_TO = dt.date(2026, 5, 3)
FREEZE_DAYS = 10                     # closed week is final this long after its Sunday
RECHECK_HOURS = 24                   # until frozen, closed weeks refresh at most daily
REMOVED_ROPP_TAGS = {"sierra": {545867780}}   # "Management Removed ROPP" (Sierra only)

COMPANIES = {
    "sierra":   {"tenant": "SIE", "tz": "pacific",  "label": "Sierra",   "color": "#1663c7"},
    "ultimate": {"tenant": "ULT", "tz": "mountain", "label": "Ultimate", "color": "#c7161d"},
    "russett":  {"tenant": "RUS", "tz": "arizona",  "label": "Russett",  "color": "#0e7a3d"},
}

# Water-heater/filtration SKU sets: Sierra's live in command_center_live
# (wh_skus); Ultimate's come from the plumb board's derived lists.
WH_SKUS = {
    "sierra": {k: {c.lower() for c in v}
               for k, v in CC_COMPANIES["sierra"]["wh_skus"].items()},
    "ultimate": {
        "tankless": {"whtl-100", "ntwh210s2", "tankless water heater install"},
        "tanks": {"50 gal water heater replacement", "40 gal water heater replacement",
                  "whng-100", "whng-120", "whng-130", "whng-190",
                  "whe-110", "whe-120", "e-wh-res-el-050"},
        "filtration": {"water softener halo", "nwscu", "nws48k", "cs-210",
                       "wtisis-100", "wtifipou-185", "nwsl"},
    },
}


# ---------------------------------------------------------------- weeks
def build_weeks(tz):
    """[(from, to)] from the deck's Week 1 through the current local week."""
    weeks = [(WEEK1_FROM, WEEK1_TO)]
    monday = WEEK1_TO + dt.timedelta(days=1)          # Mon 5/4
    today = local_today(tz)
    while monday <= today:
        weeks.append((monday, monday + dt.timedelta(days=6)))
        monday += dt.timedelta(days=7)
    return weeks


def _weekdays(day_from, day_to):
    """Mon-Fri days in the inclusive range (the deck's pacing/plan basis)."""
    n, d = 0, day_from
    while d <= day_to:
        n += d.weekday() < 5
        d += dt.timedelta(days=1)
    return n


# ---------------------------------------------------------------- per-company context
def build_ctx(company):
    """Config reusable across this company's weeks."""
    tenant = COMPANIES[company]["tenant"]

    def avoca_ids():
        return {e["id"] for e in
                fetch_all(tenant, "/settings/v2/tenant/{tenant}/employees",
                          {"active": "Any"}, page_size=200)
                if "avoca" in (e.get("name") or "").lower()}

    with ThreadPoolExecutor(max_workers=4) as pool:
        f_av = pool.submit(avoca_ids)
        f_thr = pool.submit(sold_thresholds, tenant)
        f_est = pool.submit(estimate_job_type_ids, tenant)
        f_jt = pool.submit(job_type_names, tenant)
        return {"avoca": f_av.result(),
                "thresholds": f_thr.result(), "estTypes": f_est.result(),
                "jtNames": f_jt.result()}


# ---------------------------------------------------------------- week core
def compute_week(company, day_from, day_to, ctx):
    """Every WoW metric for one company and one tenant-local week."""
    co = COMPANIES[company]
    cc = CC_COMPANIES[company]
    tenant, tz = co["tenant"], co["tz"]
    thresholds = ctx["thresholds"]
    jt_names = ctx["jtNames"]
    bu = {int(k): v for k, v in cc["bu"].items()}
    bucket = lambda j: bu.get(j.get("businessUnitId"))
    start, end = window_utc(tz, day_from, day_to)

    # ---- month panel window (the week belongs to the month its start is in;
    #      the deck kept W10 6/29-7/5 on June and closed the month with it)
    m_first = day_from.replace(day=1)
    m_last = (m_first.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1)
    mtd_to = min(day_to, m_last)
    mtd_start, mtd_end = window_utc(tz, m_first, mtd_to)

    # ---- all API pulls, concurrently (fetch_all already fans out pages)
    jobs_path = "/jpm/v2/tenant/{tenant}/jobs"
    est_path = "/sales/v2/tenant/{tenant}/estimates"
    with ThreadPoolExecutor(max_workers=7) as pool:
        f_done = pool.submit(fetch_all, tenant, jobs_path,
                             {"completedOnOrAfter": start, "completedBefore": end,
                              "jobStatus": "Completed"}, 500, 200)
        f_sold = pool.submit(fetch_all, tenant, est_path,
                             {"soldAfter": start, "soldBefore": end}, 500, 200)
        f_mtd = pool.submit(fetch_all, tenant, est_path,
                            {"soldAfter": mtd_start, "soldBefore": mtd_end}, 500, 200)
        f_appt = pool.submit(fetch_all, tenant, jobs_path,
                             {"appointmentStartsOnOrAfter": start,
                              "appointmentStartsBefore": end}, 500, 200)
        f_created = pool.submit(fetch_all, tenant, jobs_path,
                                {"createdOnOrAfter": start, "createdBefore": end}, 500, 200)
        f_calls = pool.submit(fetch_all, tenant, "/telecom/v2/tenant/{tenant}/calls",
                              {"createdOnOrAfter": start, "createdBefore": end}, 500, 400)
        jobs_done = f_done.result()
        est_sold_all = f_sold.result()
        est_mtd = f_mtd.result()
        appt_jobs = f_appt.result()
        jobs_created = f_created.result()
        calls = f_calls.result()

    not_dismissed = lambda e: ((e.get("status") or {}).get("name") or "") != "Dismissed"
    est_sold = [e for e in est_sold_all if not_dismissed(e)]

    m = {}

    # ---------------------------------------------------------- sales panel
    m["mtdSales"] = round(sum(float(e.get("subtotal") or 0) for e in est_mtd
                              if not_dismissed(e)), 2)
    m["monthKey"] = m_first.strftime("%Y-%m")
    m["wkdDone"] = _weekdays(m_first, mtd_to)
    m["wkdTotal"] = _weekdays(m_first, m_last)
    m["monthClosed"] = day_to >= m_last
    m["totSales"] = round(sum(float(e.get("subtotal") or 0) for e in est_sold), 2)

    # department split of the week's sold estimates (bucket via parent job)
    jobs_by_id = {j["id"]: j for j in jobs_done}
    for j in appt_jobs:
        jobs_by_id.setdefault(j["id"], j)
    missing = sorted({e["jobId"] for e in est_sold
                      if e.get("jobId") and e["jobId"] not in jobs_by_id})
    for i in range(0, len(missing), 50):
        for j in fetch_all(tenant, jobs_path,
                           {"ids": ",".join(map(str, missing[i:i + 50]))}, 200, 10):
            jobs_by_id[j["id"]] = j
    dept = {"sales": 0.0, "sm": 0.0, "plumb": 0.0}
    sold_on_job = {}
    for e in est_sold:
        if e.get("jobId"):
            sold_on_job.setdefault(e["jobId"], []).append(e)
        amt = float(e.get("subtotal") or 0)
        b = bucket(jobs_by_id.get(e.get("jobId")) or {})
        if b == "hvac_sales":
            dept["sales"] += amt
        elif b in HVAC_SERVICE:
            dept["sm"] += amt
        elif b in PLUMB_SERVICE:
            dept["plumb"] += amt
    m["caSales"] = round(dept["sales"], 2)
    m["smSales"] = round(dept["sm"], 2)
    m["plSales"] = round(dept["plumb"], 2)

    # ---------------------------------------------------------- call center
    st_leads = st_booked = av_leads = av_booked = 0
    seen = set()
    for c in calls:
        lc = c.get("leadCall") or {}
        cid = lc.get("id")
        if not cid or cid in seen:
            continue
        seen.add(cid)
        if lc.get("direction") != "Inbound":
            continue
        agent = (lc.get("agent") or {}).get("id") or (lc.get("createdBy") or {}).get("id")
        ct = lc.get("callType")
        if agent in ctx["avoca"]:
            av_leads += ct in LEAD_TYPES
            av_booked += ct == "Booked"
        else:
            st_leads += ct in LEAD_TYPES
            st_booked += ct == "Booked"
    m.update(stLeads=st_leads, stBooked=st_booked,
             avLeads=av_leads, avBooked=av_booked)

    # ------------------------------------------------------------- cancels
    canc = {"sm": 0, "sales": 0, "plumb": 0, "inst": 0}
    for j in appt_jobs:
        if j.get("jobStatus") != "Canceled":
            continue
        b = bucket(j)
        if b in HVAC_SERVICE:
            canc["sm"] += 1
        elif b == "hvac_sales":
            canc["sales"] += 1
        elif b in PLUMB_ALL:
            canc["plumb"] += 1
        elif b == "hvac_install":
            canc["inst"] += 1
    m.update(cancSM=canc["sm"], cancSales=canc["sales"], cancPlumb=canc["plumb"],
             cancInstall=canc["inst"], cancJobs=sum(canc.values()))

    # ------------------------------------- completed-job families (buckets)
    removed_tags = REMOVED_ROPP_TAGS.get(company, set())
    ropp_tags = set(cc["ropp_tags"])
    ropps_ran = ropps_removed = 0
    # sm = HVAC service+maint; pl = plumbing Drains/Service/Maintenance
    # (RJ's report filter - sales and membership live here); plAll adds the
    # install BU, whose big-ticket completions drive opportunity conversion
    # and the capacity denominator (the deck's plumbing numbers need it).
    fam = {k: {"jobs": 0, "opps": 0, "conv": 0, "rev": 0.0, "cust": set()}
           for k in ("sm", "pl", "plAll")}
    ca_ran = {"tgl": 0, "costco": 0, "mkt": 0}
    ca_closed = {"tgl": 0, "costco": 0, "mkt": 0}
    fam_jobs = {"sm": [], "pl": [], "plAll": []}

    # Sold sales-BU runs are auto-marked No Charge/Non-Opportunity when their
    # project is created (the revenue moves to the install ticket), so the
    # opportunity rule alone would drop exactly the sold calls. A run counts
    # when it passes the rule OR anything was ever sold on it; No Charge/
    # Non-Opp runs where nothing sold are bad leads and don't count as ran.
    # sold_on_job only sees the week's sales, so candidates that fail both
    # checks get their estimates looked up directly (a handful per week).
    sales_bad_leads = set()
    for j in jobs_done:
        if bucket(j) != "hvac_sales" or sold_on_job.get(j["id"]) \
                or is_opportunity(j, thresholds):
            continue
        ests = fetch_all(tenant, est_path, {"jobId": j["id"]}, 100, 3)
        if not any(e.get("active")
                   and ((e.get("status") or {}).get("name") or "") == "Sold"
                   for e in ests):
            sales_bad_leads.add(j["id"])
    for j in jobs_done:
        b = bucket(j)
        keys = []
        if b in HVAC_SERVICE:
            tags = set(j.get("tagTypeIds") or [])
            if ropp_tags & tags:
                if removed_tags & tags:
                    ropps_removed += 1
                else:
                    ropps_ran += 1
            keys = ["sm"]
        elif b in PLUMB_ALL:
            keys = ["plAll"] + (["pl"] if b in PLUMB_SERVICE else [])
        elif b == "hvac_sales":
            if j["id"] in sales_bad_leads:
                continue
            # Costco is BU-scoped (a TGL-typed job in the Costco BU is Costco);
            # TGL is the TGL-typed remainder of HVAC - Sales. Ultimate doesn't
            # type its sales jobs reliably, so there the lead source decides:
            # a tech-generated lead (jobGeneratedLeadSource) is TGL, no lead
            # source means the call came from marketing.
            name = (jt_names.get(j.get("jobTypeId")) or "").lower()
            if j.get("businessUnitId") in cc["costco_bu"]:
                cat = "costco"
            elif company == "ultimate":
                src = j.get("jobGeneratedLeadSource") or {}
                cat = "tgl" if src.get("employeeId") else "mkt"
            else:
                cat = "tgl" if "tgl" in name else "mkt"
            ca_ran[cat] += 1
            ca_closed[cat] += bool(sold_on_job.get(j["id"]))
            continue
        else:
            continue
        total = float(j.get("total") or 0)
        thr = thresholds.get(j.get("jobTypeId"), DEFAULT_SOLD_THRESHOLD)
        opp = (not j.get("noCharge")) or total >= thr
        for key in keys:
            f = fam[key]
            f["jobs"] += 1
            f["opps"] += opp
            f["conv"] += opp and total >= thr
            f["rev"] += total
            if j.get("customerId"):
                f["cust"].add(j["customerId"])
            fam_jobs[key].append((j, opp))
    m.update(hvacOppsRan=fam["sm"]["opps"], plumbOppsRan=fam["plAll"]["opps"],
             roppsRan=ropps_ran, roppsRemoved=ropps_removed,
             tglRan=ca_ran["tgl"], tglClosed=ca_closed["tgl"],
             costcoRan=ca_ran["costco"], costcoClosed=ca_closed["costco"],
             mktRan=ca_ran["mkt"], mktClosed=ca_closed["mkt"])

    # Membership conversion, validated against RJ's report (Sierra W10:
    # S&M 44/217 = 20%, plumbing 7/45 = 15%): memberships sold on ANY
    # previously-non-member job in the family (maintenance visits usually
    # price below the sold threshold, so the sales mostly happen on non-opp
    # jobs) over previously-non-member OPPORTUNITY jobs.
    customers = fam["sm"]["cust"] | fam["pl"]["cust"]
    memberships = _memberships_for_customers(tenant, customers) if customers else {}

    def memb_metrics(key, sku_match):
        nm_opps = nm_sold = 0
        for j, opp in fam_jobs[key]:
            if not j.get("customerId"):
                continue
            day = _local_completed_day(tz, j["completedOn"]).isoformat()
            if _member_before(memberships, j["customerId"], day):
                continue
            nm_opps += opp
            nm_sold += any(
                sku_match((it.get("sku") or {}).get("name"))
                for e in sold_on_job.get(j["id"], ())
                for it in (e.get("items") or []))
        return nm_opps, nm_sold

    m["smNmJobs"], m["smNmSold"] = memb_metrics("sm", lambda n: hvac_memb_sku(company, n))
    m["plNmJobs"], m["plNmSold"] = memb_metrics("pl", lambda n: plumb_memb_sku(company, n))

    for prefix, key in (("sm", "sm"), ("pl", "plAll")):
        f = fam[key]
        m[prefix + "Jobs"] = f["jobs"]
        m[prefix + "Opps"] = f["opps"]
        m[prefix + "Conv"] = f["conv"]
        m[prefix + "Revenue"] = round(f["rev"], 2)

    # plumbing units: WH/filtration skus on sold estimates tied to plumbing jobs
    units = {"tankless": 0, "tanks": 0, "filtration": 0}
    skus = WH_SKUS.get(company)
    if skus:
        for e in est_sold:
            if bucket(jobs_by_id.get(e.get("jobId")) or {}) not in PLUMB_ALL:
                continue  # units keep the install BU - equipment often lands there
            for it in (e.get("items") or []):
                name = " ".join(((it.get("sku") or {}).get("name") or "").split()).lower()
                for cat, names in skus.items():
                    if name in names:
                        units[cat] += max(1, int(float(it.get("qty") or 0)))
    m.update(plTankless=units["tankless"], plTanked=units["tanks"],
             plFiltration=units["filtration"])

    # ---------------------------------------------------------- HVAC silo
    # TGL = every estimate-TGL-typed job created in the week, any generating
    # tech, INCLUDING later-canceled (a created lead is a created lead, and
    # the count then never drifts after the week closes). Sierra W10: 84 vs
    # RJ's 81 - his snapshot predates a few cancel-status flips.
    tgl_jobs = []
    for j in jobs_created:
        name = (jt_names.get(j.get("jobTypeId")) or "").lower()
        if j.get("jobTypeId") in ctx["estTypes"] and "tgl" in name:
            tgl_jobs.append(j)
    appt_start = {}
    appt_ids = sorted({j.get("firstAppointmentId") for j in tgl_jobs
                       if j.get("firstAppointmentId")})
    for i in range(0, len(appt_ids), 50):
        for a in fetch_all(tenant, "/jpm/v2/tenant/{tenant}/appointments",
                           {"ids": ",".join(map(str, appt_ids[i:i + 50]))}, 200, 10):
            appt_start[a["id"]] = a.get("start")
    flips = next_day = 0
    for j in tgl_jobs:
        s = appt_start.get(j.get("firstAppointmentId"))
        if s and j.get("createdOn"):
            delta = (_local_day(tz, s) - _local_day(tz, j["createdOn"])).days
            flips += delta == 0
            next_day += delta == 1
    m.update(siloTgls=len(tgl_jobs), siloFlips=flips, siloNextDay=next_day)
    return m


# ---------------------------------------------------------------- company loop
def compute_company(company, deadline=None, progress=None):
    co = COMPANIES[company]
    tz = co["tz"]
    today = local_today(tz)
    hist = _load_json(HISTORY_FILE, {}).get(company, {})
    ctx = None
    weeks = build_weeks(tz)
    complete = True
    results = {}

    # The in-progress week refreshes every run; closed weeks backfill oldest
    # first so a budget cut always resumes deterministically.
    order = sorted(range(len(weeks)), key=lambda i: (weeks[i][1] < today, weeks[i][0]))
    for i in order:
        day_from, day_to = weeks[i]
        key = day_from.isoformat()
        ended = day_to < today
        entry = hist.get(key)
        if entry and entry.get("defs") != DEFS_VER:
            entry = None                     # stale definitions - recompute
        frozen = bool(entry and entry.get("final"))
        fresh = bool(entry and time.time() - entry.get("at", 0) < RECHECK_HOURS * 3600)
        if ended and entry and (frozen or fresh):
            results[key] = entry["m"]
            continue
        if deadline and time.time() > deadline:
            if entry:
                results[key] = entry["m"]
            else:
                complete = False
            continue
        if ctx is None:
            ctx = build_ctx(company)
        t0 = time.time()
        metrics = compute_week(company, day_from, day_to, ctx)
        if progress:
            progress(company, key, time.time() - t0)
        results[key] = metrics
        if ended:
            update_history(HISTORY_FILE, company, key, {
                "at": time.time(),
                "defs": DEFS_VER,
                "final": (today - day_to).days >= FREEZE_DAYS,
                "m": metrics,
            })

    out = []
    for i, (day_from, day_to) in enumerate(weeks):
        key = day_from.isoformat()
        if key not in results:
            continue
        out.append({
            "key": key,
            "label": f"W{i + 1}" + ("*" if i == 0 else ""),
            "from": day_from.isoformat(), "to": day_to.isoformat(),
            "range": f"{day_from.month}/{day_from.day}–{day_to.month}/{day_to.day}",
            "days": (day_to - day_from).days + 1,
            "weekdays": _weekdays(day_from, day_to),
            "ended": day_to < today,
            "m": results[key],
        })
    return {"label": co["label"], "color": co["color"],
            "plumb": bool(CC_COMPANIES[company]["plumb"]),
            "costco": bool(CC_COMPANIES[company]["costco_bu"]),
            "weeks": out, "complete": complete}


def compute(time_budget_secs=None, progress=None):
    deadline = time.time() + time_budget_secs if time_budget_secs else None
    boards = map_companies(
        lambda c: compute_company(c, deadline=deadline, progress=progress),
        COMPANIES)
    return {
        "updated": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "complete": all(b["complete"] for b in boards.values()),
        "week1Note": "Week 1 covered 3 days only (5/1–5/3).",
        "companies": {k: {kk: v[kk] for kk in
                          ("label", "color", "plumb", "costco", "weeks")}
                      for k, v in boards.items()},
    }


if __name__ == "__main__":
    import json
    data = compute(progress=lambda co, key, secs: print(f"  {co} {key} in {secs:.1f}s", flush=True))
    for cco, blob in data["companies"].items():
        last = blob["weeks"][-1] if blob["weeks"] else None
        print(cco, len(blob["weeks"]), "weeks", "last:", last and last["range"])
    print(json.dumps(data)[:400])
