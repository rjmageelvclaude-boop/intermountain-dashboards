#!/usr/bin/env python3
"""
One-shot refresh for the hosted 4 Day Call board: computes every board from
the live ServiceTitan API and writes site/call-board/data.json.

Run by .github/workflows/refresh.yml on the same cadence as the Command Center.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import call_board_live as engine

OUT_PATH = os.path.join(ROOT, "site", "call-board", "data.json")


def main():
    data = engine.compute()
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    tmp = OUT_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, OUT_PATH)
    sizes = {k: [d["opps"] for d in b["days"]] for k, b in data["boards"].items()}
    print(f"wrote {OUT_PATH}")
    print(f"opportunity calls on board by day: {json.dumps(sizes)}")


if __name__ == "__main__":
    main()
