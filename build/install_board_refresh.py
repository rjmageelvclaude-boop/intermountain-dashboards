#!/usr/bin/env python3
"""
One-shot refresh for the hosted Install Leaderboard: computes MTD/YTD install
crew KPIs from the live ServiceTitan API and writes site/install-board/data.json.

Closed months are cached in data/install-board-history.json (persisted between
runs by the Actions cache), so a normal run only recomputes the current month.
A hard time budget keeps the run inside the workflow step timeout - if the
budget runs out while backfilling closed months, the run still writes a valid
data.json (flagged complete=false) and the next run picks up where it left off.

Run by .github/workflows/refresh.yml on the same cadence as the Command Center.
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import install_board_live as engine

OUT_PATH = os.path.join(ROOT, "site", "install-board", "data.json")
TIME_BUDGET_SECS = int(os.environ.get("INSTALL_BOARD_BUDGET", "420"))


def main():
    t0 = time.time()
    data = engine.compute(
        time_budget_secs=TIME_BUDGET_SECS,
        progress=lambda co, key, secs: print(f"  {co} {key} in {secs:.1f}s", flush=True))
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    tmp = OUT_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, OUT_PATH)
    top = {c: [f"{r['name']} ${r['revenue']:,.0f}" for r in rows[:3]]
           for c, rows in data["boards"]["mtd"].items() if c != "combined"}
    print(f"wrote {OUT_PATH} in {time.time() - t0:.0f}s (complete={data['complete']})")
    print(f"MTD top 3: {json.dumps(top)}")


if __name__ == "__main__":
    main()
