#!/usr/bin/env python3
"""
One-shot refresh for the hosted Install Callback Board: computes install
cohort callback rates from the live ServiceTitan API and writes
site/callback-board/data.json.

Closed months are cached in data/callback-board-history.json (persisted
between runs by the Actions cache), so a normal run only recomputes the
current month plus any not-yet-frozen recent months. A hard time budget keeps
the run inside the workflow step timeout - if the budget runs out during a
cold backfill, the run still writes a valid data.json (complete=false) and
the next run picks up where it left off.

Run by .github/workflows/refresh.yml on the same cadence as the other boards.
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import callback_board_live as engine

OUT_PATH = os.path.join(ROOT, "site", "callback-board", "data.json")
TIME_BUDGET_SECS = int(os.environ.get("CALLBACK_BOARD_BUDGET", "180"))


def main():
    t0 = time.time()
    data = engine.compute(
        time_budget_secs=TIME_BUDGET_SECS,
        progress=lambda co, key, secs: print(f"  {co} {key} in {secs:.1f}s",
                                             flush=True))
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    tmp = OUT_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, OUT_PATH)
    kpis = {c: b["kpis"]["rate180"] for c, b in data["boards"].items()}
    print(f"wrote {OUT_PATH} in {time.time() - t0:.0f}s "
          f"(complete={data['complete']})")
    print(f"180-day callback rates: {json.dumps(kpis)}")


if __name__ == "__main__":
    main()
