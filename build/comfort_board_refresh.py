#!/usr/bin/env python3
"""
One-shot refresh for the hosted Comfort Report board: computes the current
month's comfort-advisor and technician-turnover funnels for every company
from the live ServiceTitan API and writes site/comfort-board/data.json.

Run by .github/workflows/refresh.yml on the same cadence as the other boards.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import comfort_board_live as engine

OUT_PATH = os.path.join(ROOT, "site", "comfort-board", "data.json")


def main():
    data = engine.compute()
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    tmp = OUT_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, OUT_PATH)
    tops = {co: (b["advisors"][0]["name"] if b["advisors"] else "-")
            for co, b in data["companies"].items()}
    print(f"wrote {OUT_PATH}")
    print(f"MTD revenue leaders: {json.dumps(tops)}")


if __name__ == "__main__":
    main()
