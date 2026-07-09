#!/usr/bin/env python3
"""
Local web server for the InterMountain Command Center (live ServiceTitan feed).

Serves:
    GET /            -> command-center-live/index.html (the dashboard)
    GET /data        -> {"current": {...}, "history": {...}} straight from the ST API
    GET /data?refresh=1 -> same, but forces a fresh pull (ignores the cache TTL)

Run it:
    py build/command_center_server.py            # port 8778
    py build/command_center_server.py --port 9000

Today's numbers are cached for CACHE_TTL_MIN minutes; the sparkline history is
built once per day (past days never change) and stored in data/ (git-ignored).
No Gmail / Apps Script involved anywhere.
"""
import argparse
import json
import os
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import command_center_live as engine

DASHBOARD_HTML = os.path.join(ROOT, "command-center-live", "index.html")
CACHE_TTL_MIN = 10

_lock = threading.Lock()
_cache = {"payload": None, "fetched_at": 0.0, "refreshing": False}


def _build_payload():
    current = engine.compute_current()
    history = engine.read_history()  # whatever the backfill thread has cached so far
    return {"current": current, "history": history}


def get_payload(force=False):
    while True:
        with _lock:
            have = _cache["payload"] is not None
            fresh = have and (time.time() - _cache["fetched_at"]) < CACHE_TTL_MIN * 60
            if have and (fresh and not force or _cache["refreshing"]):
                return _cache["payload"]  # serve cached rather than stampede the API
            if not _cache["refreshing"]:
                _cache["refreshing"] = True
                break
        time.sleep(1)  # first-ever load while another thread builds it
    try:
        payload = _build_payload()
        with _lock:
            _cache["payload"] = payload
            _cache["fetched_at"] = time.time()
        return payload
    finally:
        with _lock:
            _cache["refreshing"] = False


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        try:
            if url.path in ("/", "/index.html"):
                with open(DASHBOARD_HTML, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            elif url.path == "/data":
                force = parse_qs(url.query).get("refresh", ["0"])[0] == "1"
                payload = get_payload(force=force)
                self._send(200, json.dumps(payload, default=str).encode("utf-8"), "application/json")
            elif url.path == "/health":
                self._send(200, b'{"ok":true}', "application/json")
            else:
                self._send(404, b"not found", "text/plain")
        except Exception:
            err = traceback.format_exc()
            print(err, file=sys.stderr)
            self._send(500, json.dumps({"error": err.splitlines()[-1]}).encode("utf-8"), "application/json")

    def log_message(self, fmt, *args):
        # Quieter logs: one line per request without the default noise.
        print(f"[{time.strftime('%H:%M:%S')}] {self.address_string()} {fmt % args}")


def warm_in_background():
    def _warm():
        try:
            print("Warming cache: pulling today's live numbers...")
            get_payload(force=True)
            print("Today's numbers ready - backfilling sparkline history...")
            engine.compute_history(progress=lambda co, d: print(f"  history {co} {d}"))
            with _lock:  # fold the completed history into the served payload
                if _cache["payload"]:
                    _cache["payload"]["history"] = engine.read_history()
            print("History backfill complete.")
        except Exception as e:
            print(f"Warm-up failed (will retry on first request): {e}", file=sys.stderr)
    threading.Thread(target=_warm, daemon=True).start()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8778)
    args = ap.parse_args()
    warm_in_background()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"InterMountain Command Center (live API) -> http://localhost:{args.port}/")
    srv.serve_forever()
