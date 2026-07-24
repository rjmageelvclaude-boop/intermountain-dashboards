#!/usr/bin/env python3
"""
One-shot refresh for the HOSTED Command Center: compute all live metrics from
the ServiceTitan API and write site/command-center/data.json for GitHub Pages.

Run by .github/workflows/refresh.yml every 15 minutes (and works locally too):
    py build/command_center_refresh.py

In CI the ServiceTitan credentials come from the ST_SECRETS_JSON repo secret,
which the workflow writes to secrets/servicetitan.json before this runs.
The sparkline history cache (data/command-center-history.json) is restored
from the Actions cache, so each run only computes days it hasn't seen.
"""
import datetime as dt
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import command_center_live as engine
import command_center_weather as weather

OUT = os.path.join(ROOT, "site", "command-center", "data.json")


def main():
    t0 = time.time()
    print("Backfilling history (cached days are free)...")
    history = engine.compute_history(progress=lambda co, d: print(f"  {co} {d}"))
    print("Pulling today's live numbers...")
    current = engine.compute_current()
    try:
        wx = weather.get_weather()  # cached 3h; never worth failing the refresh over
    except Exception as e:
        print(f"Weather fetch failed (footer will hide): {e}")
        wx = {}
    payload = {
        "current": current,
        "history": history,
        "weather": wx,
        "generatedAt": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    print(f"Wrote {OUT} in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
