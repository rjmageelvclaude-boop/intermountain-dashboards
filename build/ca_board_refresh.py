#!/usr/bin/env python3
"""
One-shot refresh for the hosted Comfort Advisor board: computes MTD + YTD
leaderboards for every company from the live ServiceTitan API and writes
site/ca-board/data.json.

Run by .github/workflows/refresh.yml on the same cadence as the Command Center.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import ca_board_live as engine

OUT_PATH = os.path.join(ROOT, "site", "ca-board", "data.json")


def main():
    data = engine.compute()
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    tmp = OUT_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, OUT_PATH)
    leaders = {co: b["periods"]["ytd"]["advisors"][0]["name"]
               for co, b in data["companies"].items()
               if b["periods"]["ytd"]["advisors"]}
    print(f"wrote {OUT_PATH}")
    print(f"YTD leaders: {json.dumps(leaders)}")


if __name__ == "__main__":
    main()
