#!/usr/bin/env python3
"""
One-shot refresh for the hosted HVAC Install Gross Margin board: computes the
per-job gross margin report for every month of the year from the live
ServiceTitan API and writes site/gm-board/data.json.

Closed months are cached in data/gm-board-history.json (persisted between
runs by the Actions cache) and refrozen 45 days past month-end - Costco fee
invoices and payroll adjustments trickle in for weeks, so closed months are
rechecked daily until then. The current month is recomputed on every run; a
hard time budget keeps the run inside the workflow step timeout.

Run by .github/workflows/refresh.yml on the same cadence as the other boards.
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import gm_board_live as engine

OUT_PATH = os.path.join(ROOT, "site", "gm-board", "data.json")
TIME_BUDGET_SECS = int(os.environ.get("GM_BOARD_BUDGET", "420"))


def main():
    t0 = time.time()
    data = engine.compute(
        time_budget_secs=TIME_BUDGET_SECS,
        progress=lambda key, val: print(f"  {key}: {val}", flush=True))
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    tmp = OUT_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, OUT_PATH)
    cur = next((m for m in data["months"] if m["key"] == data["current"]), None)
    if cur:
        s = cur["summary"]
        print(f"current month: {s['n']} jobs, {s['total']:,.0f} revenue, "
              f"report GM {s['reportGm']*100:.1f}%, est book GM {s['estBookGm']*100:.1f}%")
    print(f"wrote {OUT_PATH} in {time.time() - t0:.0f}s "
          f"(months={len(data['months'])}, complete={data['complete']})")


if __name__ == "__main__":
    main()
