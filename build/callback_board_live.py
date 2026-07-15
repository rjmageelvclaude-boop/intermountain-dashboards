#!/usr/bin/env python3
"""
Live ServiceTitan engine for the Install Callback Board.

How often does the HVAC install department go back to a finished install?
Callbacks land anywhere from 1 to 180+ days after the install, so a plain
monthly rate is misleading - young months always look better than old ones.
The board therefore uses install-month COHORTS: each month's installs are
tracked for callbacks within 30/60/90/180 days, and a window is only marked
"final" once every install in the cohort has had that long to fail.

Scope: the HVAC install business unit of each company.

    sierra     SIE  BU 337        HVAC - Install - AOR
    ultimate   ULT  BU 12932      HVAC - Install - AOR
    russett    RUS  BU 42371009   HVAC - Install - AOR
    brothers   BRO  BU 2218902    HVAC Install

Classification (RJ's rules, 2026-07-15). jpm/v2 ignores businessUnitIds /
jobTypeIds query params server-side, so every completed job in the month is
fetched and filtered client-side:

    install   install-BU, revenue-bearing, not any category below. These are
              the cohort members (true system/equipment installs).
    recall    typed callbacks: Recall/Warranty, Retro Finish, Startup,
              Client Resolution, Recall Install. Retro Finish counts as a
              recall because the crew could not finish on the install date.
              Counted from the install BU always; from any other BU only
              when they link back to one of our installs.
    part      "Install HVAC Part"-style jobs. Counted (recall bucket) only
              when they follow an install - audited 6/2026: 80%+ are
              warranty part return trips. Standalone part jobs are ignored.
    service   ANY other non-excluded job, any non-plumbing BU, that lands at
              the same project/location within 1-180 days after one of our
              installs (created after the install completed). This catches
              install problems booked as plain service demand calls.
    excluded  plumbing BUs, Drive By, membership tune-ups / maintenance,
              estimates, Quality Assurance / QA crew checks and drywall
              (planned post-install visits; QA+drywall still counted for
              the footer).

A callback links to its install by recallForId -> same project -> same
location (latest install completed on or before the callback was created;
3-day grace for typed recalls, none for service returns). Linking happens at
fetch time, oldest month first, so every month sees all earlier installs.

Per install we also fetch the assigned crew (appointment-assignments) for
the per-installer callback rate, and per callback the appointment durations
for return-trip labor hours. Reason coding is regexed from the job summary.

Closed months are cached in data/callback-board-history.json (schema v2),
recomputed at most daily until 40 days past month-end, then frozen. Current
month always recomputes. Rolling window: current + 18 closed months.

CLI smoke test:
    py build/callback_board_live.py                # summary, all companies
    py build/callback_board_live.py sierra 2026-06 # one company-month, raw
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
from command_center_live import (fetch_all, local_today, _load_json,
                                 map_companies, update_history)
from tech_board_live import month_window_utc

HISTORY_FILE = os.path.join(ROOT, "data", "callback-board-history.json")
CACHE_V = 4                  # bump when classification/schema changes
WINDOW_CLOSED_MONTHS = 18    # cohort months kept besides the current month
MONTH_FREEZE_DAYS = 40       # month is final this long after month-end
MONTH_RECHECK_HOURS = 24     # until frozen, closed months refresh at most daily
OPEN_CB_LOOKBACK_DAYS = 75   # created-window scanned for still-open callbacks
WINDOWS = (30, 60, 90, 180)
RECENT_LIMIT = 60            # rows in the recent-callbacks table per company
SERVICE_MAX_GAP = 180        # service returns only count this close to install
CREW_MIN_INSTALLS = 8        # min mature installs before a tech shows on the board

COMPANIES = {
    "sierra":   {"tenant": "SIE", "tz": "pacific",  "label": "Sierra",
                 "color": "#1663c7", "bu": 337},
    "ultimate": {"tenant": "ULT", "tz": "mountain", "label": "Ultimate",
                 "color": "#c7161d", "bu": 12932},
    "russett":  {"tenant": "RUS", "tz": "arizona",  "label": "RSW",
                 "color": "#0e7a3d", "bu": 42371009},
    "brothers": {"tenant": "BRO", "tz": "mountain", "label": "Brothers",
                 "color": "#c2410c", "bu": 2218902},
}

# ---------------------------------------------------------------- classify
RE_PLUMB = re.compile(r"plumb|sewer|\bdrains?\b", re.I)
# non-HVAC trade BUs: service returns from these are someone else's work
# (typed recalls still count when they link back to one of our installs)
RE_TRADE_BU = re.compile(r"plumb|electric|excavat|landscap|solar|sewer|drain", re.I)
RE_QA = re.compile(r"quality assurance|qa crew|q/a", re.I)
RE_DRYWALL = re.compile(r"drywall", re.I)
RE_DRIVEBY = re.compile(r"drive ?by", re.I)
# membership / planned visits: tune-ups, PSC/PPS planned service checks,
# check-ups, inspections, duct cleaning, filter changes
RE_MAINT = re.compile(r"tune|maint|membership|\bmsa\b|club|precision|filter"
                      r"|check ?up|inspect|duct clean|safety|rejuv"
                      r"|\bpsc\b|\bpps\b|\bsam maint", re.I)
RE_EST = re.compile(r"estimate|second opinion", re.I)
# type names that pin the serviced system as YEARS old - can't be the
# install we did months ago ("AC Issue 8+ yrs", "HT PSC 4-7", ...)
RE_OLDSYS = re.compile(r"\b4\s*-\s*7\b|\b[456789]\s*\+|\b1[05]\s*\+", re.I)
# plumbing-trade work booked outside a plumbing-named BU
RE_PLUMB_TYPE = re.compile(r"water heater|softener|sewer|camera|repipe|faucet"
                           r"|toilet|sink\b|shower|bathtub|gas line|filtration"
                           r"|sprinkler|irrigation|drain clean|main line", re.I)
# new-construction phase visits (RUS "NC - ..." types): planned, not callbacks
RE_NEWCON = re.compile(r"\bnc\s*-|new construction|rough ?in", re.I)
RE_RECALL = re.compile(r"recall|warranty|retro finish|client resolution", re.I)
RE_STARTUP = re.compile(r"start\s*-?\s*up", re.I)
RE_PART = re.compile(r"\bparts?\b", re.I)


def classify(type_name, bu_name=""):
    """excluded | qa | drywall | recall | part | neutral.
    neutral resolves by BU: install BU + revenue -> install (cohort);
    anything else -> service-return candidate (counts only when linked)."""
    n = (type_name or "").strip()
    if RE_PLUMB.search(bu_name or ""):
        return "excluded"
    if RE_QA.search(n):
        return "qa"
    if RE_DRYWALL.search(n):
        return "drywall"
    if (RE_DRIVEBY.search(n) or RE_MAINT.search(n) or RE_EST.search(n)
            or RE_OLDSYS.search(n) or RE_PLUMB_TYPE.search(n)
            or RE_NEWCON.search(n)):
        return "excluded"
    if RE_RECALL.search(n) or RE_STARTUP.search(n):
        return "recall"
    if RE_PART.search(n):
        return "part"
    return "neutral"


# reason buckets, first match wins (type name + summary, html stripped)
REASONS = [
    ("Parts on order",        r"warranty part|parts? (on order|ordered|arriv|in\b)"
                              r"|waiting on|special order|back ?order|part instl"),
    ("No cool / no heat",     r"no cool|not cool|no heat|not heat|blowing (warm|hot)"
                              r"|warm air|not work|stopped work|won'?t (turn|come|kick)"
                              r"|not blowing|not turning"),
    ("Leak / drain / water",  r"leak|drain|dripp|condensat|water (damage|on|in)\b"),
    ("Thermostat / controls", r"thermostat|t-?stat|nest\b|nuve|sensor"),
    ("Electrical / breaker",  r"breaker|electric|wiring|rewire|fuse|\bamps?\b|voltage"
                              r"|disconnect"),
    ("Airflow / ductwork",    r"air ?flow|duct|register|grille?|\bvents?\b|damper|balanc"),
    ("Refrigerant / sealed",  r"refrigerant|freon|recharge|410a?|454b|txv|compressor"),
    ("Noise / vibration",     r"noise|noisy|loud|rattl|vibrat|humming|squeal|banging"),
    ("Commission / startup",  r"start ?up|commission|test (heat|cool)|wire and test"
                              r"|inspect"),
    ("Goodwill / comfort",    r"inconvenience|goodwill|courtesy|comfort"),
]
REASONS = [(lbl, re.compile(rx, re.I)) for lbl, rx in REASONS]
RE_TAGS = re.compile(r"<[^>]+>")


def reason(type_name, summary, cat):
    txt = RE_TAGS.sub(" ", f"{type_name or ''} {summary or ''}")
    for label, rx in REASONS:
        if rx.search(txt):
            return label
    if cat == "part":
        return "Parts on order"
    return "Other / unspecified"


# equipment category from the install job-type name
EQUIP = [("Package", r"package"), ("Heat Pump", r"heat pump"),
         ("Mini Split", r"mini ?split"), ("Furnace 80%", r"80%"),
         ("Furnace 90%", r"90%|96%"), ("Condenser / Coil", r"condenser|\bcoil\b"),
         ("Ductwork", r"duct"), ("Aquatherm", r"aquatherm")]
EQUIP = [(lbl, re.compile(rx, re.I)) for lbl, rx in EQUIP]


def equip_cat(type_name):
    for label, rx in EQUIP:
        if rx.search(type_name or ""):
            return label
    return "Other"


# ---------------------------------------------------------------- fetch
_JOB_TYPES, _BU_NAMES, _TECHS = {}, {}, {}


def job_types(tenant):
    """{jobTypeId: name}, including inactive/archived types (an unnamed type
    can't be excluded, so it would leak into the service bucket)."""
    if tenant not in _JOB_TYPES:
        try:
            rows = fetch_all(tenant, "/jpm/v2/tenant/{tenant}/job-types",
                             {"active": "Any"}, page_size=200)
        except Exception:
            rows = fetch_all(tenant, "/jpm/v2/tenant/{tenant}/job-types",
                             {}, page_size=200)
        _JOB_TYPES[tenant] = {t["id"]: t.get("name") or "" for t in rows}
    return _JOB_TYPES[tenant]


def bu_names(tenant):
    if tenant not in _BU_NAMES:
        _BU_NAMES[tenant] = {b["id"]: b.get("name") or "" for b in fetch_all(
            tenant, "/settings/v2/tenant/{tenant}/business-units",
            {"active": "Any"}, page_size=100)}
    return _BU_NAMES[tenant]


def tech_names(tenant):
    """{technicianId: cleaned display name} (active + inactive)."""
    if tenant not in _TECHS:
        out = {}
        for t in fetch_all(tenant, "/settings/v2/tenant/{tenant}/technicians",
                           {"active": "Any"}, page_size=200):
            name = (t.get("name") or "").strip()
            if name.upper().endswith("-TECH"):
                name = name[:-5].rstrip(" -")
            out[t["id"]] = name
        _TECHS[tenant] = out
    return _TECHS[tenant]


def _day(ts):
    return ts[:10] if ts else None


def _parse(day):
    return dt.date(int(day[:4]), int(day[5:7]), int(day[8:10]))


# install index shared across months (built oldest -> newest, so lists stay
# sorted by completion date without re-sorting)
def new_index():
    return {"id": {}, "proj": {}, "loc": {}}


def index_installs(idx, installs):
    for i in installs:
        idx["id"][i["i"]] = i
        if i.get("proj"):
            idx["proj"].setdefault(i["proj"], []).append(i)
        idx["loc"].setdefault(i["loc"], []).append(i)


def _link(idx, rf, proj, loc, ref_day, grace_days):
    """Latest install completed on or before ref_day (+grace)."""
    if rf in idx["id"]:
        return idx["id"][rf]
    if not ref_day:
        return None
    limit = (_parse(ref_day) + dt.timedelta(days=grace_days)).isoformat()
    for key, val in (("proj", proj), ("loc", loc)):
        if val is None:
            continue
        cands = [i for i in idx[key].get(val, ()) if i["d"] <= limit]
        if cands:
            return cands[-1]
    return None


def month_events(company, year, month, idx):
    """Classified + linked events for jobs COMPLETED in the month.

    Mutates idx with this month's installs. Returns
    {"installs": [...], "callbacks": [...], "qa": n, "drywall": n}.
    """
    co = COMPANIES[company]
    tenant, bu = co["tenant"], co["bu"]
    start, end = month_window_utc(co["tz"], year, month)
    jt, bus = job_types(tenant), bu_names(tenant)

    jobs = fetch_all(tenant, "/jpm/v2/tenant/{tenant}/jobs",
                     {"completedOnOrAfter": start, "completedBefore": end,
                      "jobStatus": "Completed"},
                     page_size=500, max_pages=400)
    jobs.sort(key=lambda j: j.get("completedOn") or "")

    # pass 1: cohort installs into the index, so same-month callbacks link
    installs, appt_of_inst = [], {}
    for j in jobs:
        tname = jt.get(j.get("jobTypeId"))
        if (j.get("businessUnitId") == bu and float(j.get("total") or 0) > 0
                and classify(tname, bus.get(bu)) == "neutral"):
            rec = {"i": j["id"], "d": _day(j.get("completedOn")),
                   "loc": j.get("locationId"), "proj": j.get("projectId"),
                   "t": round(float(j["total"]), 2), "eq": equip_cat(tname),
                   "tc": []}
            installs.append(rec)
            appt_of_inst[j["id"]] = [a for a in (j.get("firstAppointmentId"),
                                                 j.get("lastAppointmentId")) if a]
    index_installs(idx, installs)

    # pass 2: callbacks
    callbacks, appt_of_cb = [], {}
    qa = drywall = 0
    for j in jobs:
        tname = jt.get(j.get("jobTypeId"))
        jbu = j.get("businessUnitId")
        cat = classify(tname, bus.get(jbu))
        if cat == "excluded":
            continue
        if cat == "qa":
            qa += jbu == bu
            continue
        if cat == "drywall":
            drywall += jbu == bu
            continue
        if cat == "neutral" and jbu == bu and float(j.get("total") or 0) > 0:
            continue                      # cohort install, handled above

        ref = _day(j.get("createdOn")) or _day(j.get("completedOn"))
        d = _day(j.get("completedOn"))
        if cat in ("recall", "part"):
            orig = _link(idx, j.get("recallForId"), j.get("projectId"),
                         j.get("locationId"), ref, grace_days=3)
            # typed recalls in OUR install BU always count (old-install
            # warranty stays workload); other BUs / part jobs need a link
            if orig is None and not (cat == "recall" and jbu == bu):
                continue
            bucket = "recall"
        else:                             # neutral -> service-return candidate
            if RE_TRADE_BU.search(bus.get(jbu) or ""):
                continue                  # other-trade demand work, not ours
            orig = _link(idx, None, j.get("projectId"), j.get("locationId"),
                         ref, grace_days=0)
            if orig is None or not d:
                continue
            gap = (_parse(d) - _parse(orig["d"])).days
            if gap < 1 or gap > SERVICE_MAX_GAP:
                continue
            bucket = "service"

        cb = {"i": j["id"], "b": bucket, "ty": tname or "?", "d": d,
              "rsn": reason(tname, j.get("summary"), cat),
              "oi": None, "om": None, "gap": None, "hrs": 0}
        if orig is not None and d:
            cb["oi"] = orig["i"]
            cb["om"] = orig["d"][:7]
            cb["gap"] = max(0, (_parse(d) - _parse(orig["d"])).days)
        callbacks.append(cb)
        appt_of_cb[j["id"]] = [a for a in (j.get("firstAppointmentId"),
                                           j.get("lastAppointmentId")) if a]

    _fill_crews(tenant, installs, appt_of_inst)
    _fill_hours(tenant, callbacks, appt_of_cb)
    return {"installs": installs, "callbacks": callbacks,
            "qa": qa, "drywall": drywall}


def _fill_crews(tenant, installs, appt_of):
    """installs[i]["tc"] = assigned technician ids (first+last appointment)."""
    appt_to_inst = {}
    for rec in installs:
        for a in appt_of.get(rec["i"], ()):
            appt_to_inst[a] = rec
    ids = sorted(appt_to_inst)
    techs = {rec["i"]: set() for rec in installs}
    for i in range(0, len(ids), 40):
        batch = ",".join(str(a) for a in ids[i:i + 40])
        for a in fetch_all(tenant,
                           "/dispatch/v2/tenant/{tenant}/appointment-assignments",
                           {"appointmentIds": batch}, page_size=200, max_pages=10):
            if not a.get("active"):
                continue
            rec = appt_to_inst.get(a.get("appointmentId"))
            if rec is not None:
                techs[rec["i"]].add(a["technicianId"])
    for rec in installs:
        rec["tc"] = sorted(techs[rec["i"]])


def _fill_hours(tenant, callbacks, appt_of):
    """callbacks[i]["hrs"] = scheduled appointment hours (first+last appt)."""
    appt_ids = sorted({a for appts in appt_of.values() for a in appts})
    dur = {}
    for i in range(0, len(appt_ids), 50):
        for a in fetch_all(tenant, "/jpm/v2/tenant/{tenant}/appointments",
                           {"ids": ",".join(map(str, appt_ids[i:i + 50]))},
                           page_size=200, max_pages=10):
            s, e = a.get("start"), a.get("end")
            if s and e:
                try:
                    t0 = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
                    t1 = dt.datetime.fromisoformat(e.replace("Z", "+00:00"))
                    dur[a["id"]] = max((t1 - t0).total_seconds() / 3600, 0)
                except ValueError:
                    pass
    for cb in callbacks:
        cb["hrs"] = round(sum(dur.get(a, 0) for a in appt_of.get(cb["i"], ())), 1)


def open_callbacks(company):
    """Typed recall/part jobs booked but not yet completed (created in the
    last OPEN_CB_LOOKBACK_DAYS), in the install BU."""
    co = COMPANIES[company]
    since = (dt.datetime.utcnow()
             - dt.timedelta(days=OPEN_CB_LOOKBACK_DAYS)).strftime(
                 "%Y-%m-%dT00:00:00Z")
    jt, bus = job_types(co["tenant"]), bu_names(co["tenant"])
    n = 0
    for j in fetch_all(co["tenant"], "/jpm/v2/tenant/{tenant}/jobs",
                       {"createdOnOrAfter": since}, page_size=500,
                       max_pages=400):
        if (j.get("businessUnitId") == co["bu"]
                and j.get("jobStatus") not in ("Completed", "Canceled")
                and classify(jt.get(j.get("jobTypeId")),
                             bus.get(co["bu"])) in ("recall", "part")):
            n += 1
    return n


# ---------------------------------------------------------------- caching
def _month_key(year, month):
    return f"{year:04d}-{month:02d}"


def window_months(company):
    """[(y, m)] oldest->newest: 18 closed months + the current month."""
    today = local_today(COMPANIES[company]["tz"])
    y, m = today.year, today.month
    out = []
    for _ in range(WINDOW_CLOSED_MONTHS + 1):
        out.append((y, m))
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return list(reversed(out)), today


def compute_company(company, deadline=None, progress=None):
    """{month_key: events} across the window, cached. Returns (months, complete).

    Walks oldest -> newest so the install index always contains every earlier
    install when a month's callbacks link. A month fetched after a
    budget-skip is written un-freezable (at=0) so it recomputes next run with
    the full index.
    """
    cache = _load_json(HISTORY_FILE, {}).get(company, {})
    months, today = window_months(company)
    current_key = _month_key(today.year, today.month)
    idx = new_index()
    result, complete = {}, True
    ctx_gap = False               # a prior month was skipped this run

    for year, month in months:
        key = _month_key(year, month)
        entry = cache.get(key)
        if entry and entry.get("v") == CACHE_V and key != current_key:
            month_end = dt.date(year + (month == 12), month % 12 + 1, 1)
            frozen = entry.get("final") and (today - month_end).days >= MONTH_FREEZE_DAYS
            fresh = time.time() - entry.get("at", 0) < MONTH_RECHECK_HOURS * 3600
            if frozen or fresh:
                result[key] = entry["events"]
                index_installs(idx, entry["events"]["installs"])
                continue
        if deadline and time.time() > deadline and key != current_key:
            complete = False          # out of budget - next run resumes here
            ctx_gap = True
            if entry and entry.get("v") == CACHE_V:
                result[key] = entry["events"]
                index_installs(idx, entry["events"]["installs"])
            continue
        t0 = time.time()
        events = month_events(company, year, month, idx)
        result[key] = events
        if key != current_key:
            month_end = dt.date(year + (month == 12), month % 12 + 1, 1)
            update_history(HISTORY_FILE, company, key, {
                "at": 0 if ctx_gap else time.time(), "events": events,
                "v": CACHE_V,
                "final": (not ctx_gap
                          and (today - month_end).days >= MONTH_FREEZE_DAYS)})
        if progress:
            progress(company, key, time.time() - t0)
    return result, complete


# ---------------------------------------------------------------- aggregate
def _blank_cohort():
    c = {"installs": 0, "visits": 0, "recall": 0, "service": 0}
    c.update({f"u{w}": 0 for w in WINDOWS})
    return c


def aggregate(months, today):
    """cohorts, monthly trend, curve, gap histogram, reasons and equipment
    mix for one company (or pre-merged combined months)."""
    keys = sorted(months)
    linked = {}
    for ev in months.values():
        for cb in ev["callbacks"]:
            if cb.get("oi") is not None:
                linked.setdefault(cb["oi"], []).append(cb)

    cohorts = {k: _blank_cohort() for k in keys}
    curve_n = 0                       # installs old enough for the full curve
    curve = [0] * 181                 # first-callback count by gap day
    equip = {}
    for k in keys:
        for inst in months[k]["installs"]:
            c = cohorts[k]
            c["installs"] += 1
            cbs = linked.get(inst["i"], ())
            c["visits"] += len(cbs)
            c["recall"] += sum(1 for x in cbs if x["b"] == "recall")
            c["service"] += sum(1 for x in cbs if x["b"] == "service")
            for w in WINDOWS:
                if any(x["gap"] <= w for x in cbs):
                    c[f"u{w}"] += 1
            age = (today - _parse(inst["d"])).days
            if age > 180:
                curve_n += 1
                first = min((x["gap"] for x in cbs), default=None)
                if first is not None and first <= 180:
                    curve[first] += 1
            eq = equip.setdefault(inst.get("eq") or "Other",
                                  {"cat": inst.get("eq") or "Other",
                                   "inst": 0, "n90": 0, "u90": 0,
                                   "n180": 0, "u180": 0})
            eq["inst"] += 1
            if age > 90:
                eq["n90"] += 1
                eq["u90"] += any(x["gap"] <= 90 for x in cbs)
            if age > 180:
                eq["n180"] += 1
                eq["u180"] += any(x["gap"] <= 180 for x in cbs)
    for i in range(1, 181):
        curve[i] += curve[i - 1]      # cumulative

    cohort_rows = []
    for k in keys:
        y, m = int(k[:4]), int(k[5:7])
        month_end = dt.date(y + (m == 12), m % 12 + 1, 1) - dt.timedelta(days=1)
        c = cohorts[k]
        c["month"] = k
        c["mature"] = {str(w): (today - month_end).days >= w for w in WINDOWS}
        cohort_rows.append(c)

    # monthly workload trend + gap histogram + reasons + recent list
    monthly, hist = [], {"0-7": 0, "8-30": 0, "31-60": 0, "61-90": 0,
                         "91-180": 0, "180+": 0}
    recent, gaps_12mo, reasons = [], [], {}
    yr_ago = (today - dt.timedelta(days=365)).isoformat()
    for k in keys:
        ev = months[k]
        rec = sum(1 for x in ev["callbacks"] if x["b"] == "recall")
        svc = len(ev["callbacks"]) - rec
        hrs = sum(x.get("hrs") or 0 for x in ev["callbacks"])
        # normalize by the installs those callbacks can come from:
        # this month + the 5 before it (~ the 180-day tail); months without
        # a full 6-month pool behind them get no rate rather than a fake one
        pool = sum(cohorts[p]["installs"] for p in keys
                   if p <= k and (int(k[:4]) * 12 + int(k[5:7]))
                   - (int(p[:4]) * 12 + int(p[5:7])) < 6)
        full_pool = keys.index(k) >= 5
        monthly.append({"month": k, "visits": rec + svc, "recall": rec,
                        "service": svc, "qa": ev["qa"], "drywall": ev["drywall"],
                        "installs": cohorts[k]["installs"],
                        "hrs": round(hrs, 1),
                        "per100": round((rec + svc) / pool * 100, 1)
                                  if pool and full_pool else None})
        for x in ev["callbacks"]:
            g = x.get("gap")
            if g is not None:
                hist["0-7" if g <= 7 else "8-30" if g <= 30 else "31-60"
                     if g <= 60 else "61-90" if g <= 90 else "91-180"
                     if g <= 180 else "180+"] += 1
            if x["d"] and x["d"] >= yr_ago:
                if g is not None:
                    gaps_12mo.append(g)
                reasons[x["rsn"]] = reasons.get(x["rsn"], 0) + 1
            recent.append({"date": x["d"], "type": x["ty"], "cat": x["b"],
                           "rsn": x["rsn"], "gap": g, "om": x.get("om")})
    recent.sort(key=lambda r: r["date"] or "", reverse=True)
    gaps_12mo.sort()

    equip_rows = sorted((e for e in equip.values() if e["inst"] >= 5),
                        key=lambda e: -e["inst"])
    return {
        "cohorts": cohort_rows,
        "monthly": monthly,
        "curve": {"installs": curve_n, "byDay": curve},
        "hist": hist,
        "reasons": sorted(reasons.items(), key=lambda kv: -kv[1]),
        "equip": equip_rows,
        "medianGap": gaps_12mo[len(gaps_12mo) // 2] if gaps_12mo else 0,
        "recent": recent[:RECENT_LIMIT],
    }


def crew_rows(months, names, today, label):
    """Per-installer callback rate over installs mature for 90 days."""
    linked = {}
    for ev in months.values():
        for cb in ev["callbacks"]:
            if cb.get("oi") is not None:
                linked.setdefault(cb["oi"], []).append(cb)
    stats = {}
    for ev in months.values():
        for inst in ev["installs"]:
            if (today - _parse(inst["d"])).days <= 90:
                continue
            cbs = linked.get(inst["i"], ())
            hit90 = any(x["gap"] <= 90 for x in cbs)
            for t in inst.get("tc", ()):
                s = stats.setdefault(t, {"inst": 0, "cb90": 0, "visits": 0})
                s["inst"] += 1
                s["cb90"] += hit90
                s["visits"] += len(cbs)
    rows = []
    for t, s in stats.items():
        if s["inst"] < CREW_MIN_INSTALLS:
            continue
        rows.append({"n": names.get(t) or f"Tech {t}", "co": label,
                     "inst": s["inst"],
                     "rate90": round(s["cb90"] / s["inst"] * 100, 1),
                     "per100": round(s["visits"] / s["inst"] * 100)})
    rows.sort(key=lambda r: (-r["rate90"], -r["inst"]))
    return rows


def _merge_months(named_months):
    """Merge {company: months} into one 'combined' months dict.

    Job/project/location ids are per-tenant numeric sequences that can
    collide across tenants, so every id is namespaced with the company -
    otherwise a callback could attach to another company's install.
    """
    out = {}
    for company, months in named_months.items():
        def ns(v):
            return f"{company}:{v}" if v is not None else None
        for k, ev in months.items():
            tgt = out.setdefault(k, {"installs": [], "callbacks": [],
                                     "qa": 0, "drywall": 0})
            tgt["installs"].extend(dict(i, i=ns(i["i"]), loc=ns(i["loc"]),
                                        proj=ns(i.get("proj")))
                                   for i in ev["installs"])
            tgt["callbacks"].extend(dict(c, i=ns(c["i"]), oi=ns(c.get("oi")))
                                    for c in ev["callbacks"])
            tgt["qa"] += ev["qa"]
            tgt["drywall"] += ev["drywall"]
    return out


def _kpis(agg, open_cb, today):
    """Headline numbers from the aggregated views."""
    rows = agg["cohorts"]
    cur_key = today.isoformat()[:7]

    def mature_rate(w, last_n=6):
        m = [r for r in rows if r["mature"][str(w)] and r["installs"]][-last_n:]
        inst = sum(r["installs"] for r in m)
        return (sum(r[f"u{w}"] for r in m) / inst * 100) if inst else 0, inst

    r30, _ = mature_rate(30)
    r90, _ = mature_rate(90)
    r180, n180 = mature_rate(180)
    yr = [m for m in agg["monthly"] if m["month"] != cur_key][-12:]
    visits_yr = sum(m["visits"] for m in yr)
    installs_yr = sum(m["installs"] for m in yr)
    hrs_yr = sum(m["hrs"] for m in yr)
    mtd = next((m for m in agg["monthly"] if m["month"] == cur_key),
               {"visits": 0, "recall": 0, "service": 0, "installs": 0})
    return {
        "rate30": round(r30, 1), "rate90": round(r90, 1),
        "rate180": round(r180, 1), "rate180Installs": n180,
        "visitsPer100": round(visits_yr / installs_yr * 100, 1) if installs_yr else 0,
        "visitsYr": visits_yr,
        "hrsYr": round(hrs_yr),
        "hrsPer100": round(hrs_yr / installs_yr * 100) if installs_yr else 0,
        "mtdVisits": mtd["visits"], "mtdRecall": mtd["recall"],
        "mtdService": mtd["service"], "mtdInstalls": mtd["installs"],
        "openCallbacks": open_cb,
        "medianGap": agg["medianGap"],
    }


# ---------------------------------------------------------------- public API
def compute(time_budget_secs=None, progress=None):
    deadline = time.time() + time_budget_secs if time_budget_secs else None

    def one(company):
        months, ok = compute_company(company, deadline=deadline, progress=progress)
        try:
            open_cb = open_callbacks(company)
        except Exception as e:
            print(f"WARNING: {company} open-callback scan failed ({e})", flush=True)
            open_cb = None
        try:
            names = tech_names(COMPANIES[company]["tenant"])
        except Exception as e:
            print(f"WARNING: {company} technician fetch failed ({e})", flush=True)
            names = {}
        return months, ok, open_cb, names

    results = map_companies(one, COMPANIES)
    today = local_today("pacific")
    boards, complete = {}, True
    month_sets, open_total = {}, 0
    for company, (months, ok, open_cb, names) in results.items():
        complete = complete and ok
        month_sets[company] = months
        open_total += open_cb or 0
        agg = aggregate(months, today)
        for r in agg["recent"]:
            r["co"] = COMPANIES[company]["label"]
        boards[company] = dict(
            agg, kpis=_kpis(agg, open_cb, today),
            crew=crew_rows(months, names, today, COMPANIES[company]["label"]))

    combined = aggregate(_merge_months(month_sets), today)
    combined["recent"] = sorted(
        (r for c in COMPANIES for r in boards[c]["recent"]),
        key=lambda r: r["date"] or "", reverse=True)[:RECENT_LIMIT]
    boards["combined"] = dict(
        combined, kpis=_kpis(combined, open_total, today),
        crew=sorted((r for c in COMPANIES for r in boards[c]["crew"]),
                    key=lambda r: (-r["rate90"], -r["inst"])))

    return {
        "updated": dt.datetime.now().strftime("%a %b %d %Y %H:%M:%S"),
        "complete": complete,
        "today": today.isoformat(),
        "companies": {c: {"label": co["label"], "color": co["color"]}
                      for c, co in COMPANIES.items()},
        "windows": list(WINDOWS),
        "boards": boards,
    }


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) > 2:
        company, ym = sys.argv[1], sys.argv[2]
        y, m = map(int, ym.split("-"))
        ev = month_events(company, y, m, new_index())
        rec = sum(1 for c in ev["callbacks"] if c["b"] == "recall")
        svc = len(ev["callbacks"]) - rec
        lk = sum(1 for c in ev["callbacks"] if c.get("oi") is not None)
        print(f"{company} {ym}: {len(ev['installs'])} installs, "
              f"{len(ev['callbacks'])} callbacks ({rec} recall, {svc} service, "
              f"{lk} linked), qa {ev['qa']}, drywall {ev['drywall']}")
        for c in ev["callbacks"][:15]:
            print(f"  {c['d']} {c['b']:7s} gap={c['gap']} {c['rsn']:22s} {c['ty']}")
    else:
        t0 = time.time()
        data = compute(progress=lambda co, k, s: print(f"  {co} {k} {s:.1f}s",
                                                       flush=True))
        for c, b in data["boards"].items():
            k = b["kpis"]
            print(f"{c:9s} 30d {k['rate30']:4.1f}%  90d {k['rate90']:4.1f}%  "
                  f"180d {k['rate180']:4.1f}%  visits/100 {k['visitsPer100']:5.1f}  "
                  f"hrs/100 {k['hrsPer100']:4d}  open {k['openCallbacks']}")
        print(f"-- computed in {time.time() - t0:.0f}s "
              f"(complete={data['complete']})")
