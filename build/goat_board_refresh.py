#!/usr/bin/env python3
"""
One-shot refresh for the hosted GOAT Group board: assembles every
department's YTD percent-to-goal rows and writes site/goat-board/data.json.

Four departments are read from the sibling boards' data.json files, so this
must run AFTER ca/tech/plumb/install board refreshes in the workflow. The
two live-computed groups (SILO techs, plumbing installers) cache closed
months in data/goat-board-history.json under a hard time budget, same
resume-next-run pattern as the tech board.

Run by .github/workflows/refresh.yml on the same cadence as the other boards.
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import goat_board_live as engine

OUT_PATH = os.path.join(ROOT, "site", "goat-board", "data.json")
TIME_BUDGET_SECS = int(os.environ.get("GOAT_BOARD_BUDGET", "420"))


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
    leaders = {d["key"]: (f"{d['people'][0]['name']} ${d['people'][0]['amount']:,.0f}"
                          if d["people"] else "-")
               for d in data["departments"]}
    print(f"wrote {OUT_PATH} in {time.time() - t0:.0f}s (complete={data['complete']})")
    print(f"leaders: {json.dumps(leaders)}")


if __name__ == "__main__":
    main()
