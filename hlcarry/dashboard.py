"""Local dashboard: http://127.0.0.1:8765

Serves one HTML page plus a JSON endpoint that merges the paper loop's
state file, the watcher's alerts, the kill switch and a little live market
context (hourly candles and funding history), cached for a minute.
Binds to localhost only. Read-only: there are no buttons that trade.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .api import InfoClient

log = logging.getLogger("dashboard")
STATIC = Path(__file__).parent / "static" / "dashboard.html"


class MarketCache:
    def __init__(self, client: InfoClient, coin: str, ttl: float = 60):
        self.client, self.coin, self.ttl = client, coin, ttl
        self._at, self._data, self._lock = 0.0, {}, threading.Lock()

    def get(self) -> dict:
        with self._lock:
            if time.time() - self._at < self.ttl:
                return self._data
            now = int(time.time() * 1000)
            try:
                candles = self.client.candles(self.coin, "1h", now - 7 * 86400_000, now)
                funding = self.client.funding_history(self.coin, now - 14 * 86400_000)
                self._data = {
                    "candles": [[c["t"], float(c["o"]), float(c["h"]), float(c["l"]), float(c["c"])] for c in candles],
                    "funding": [[int(f["time"]), float(f["fundingRate"]) * 8760] for f in funding],
                }
                self._at = time.time()
            except Exception as e:  # noqa: BLE001 - dashboard keeps serving stale data
                log.warning("market fetch failed: %r", e)
            return self._data


def build_payload(cfg: dict, market: dict) -> dict:
    state_path = Path(cfg["paper"]["state_dir"]) / "state.json"
    try:
        state = json.loads(state_path.read_text())
    except (OSError, ValueError):
        state = None
    alerts_path = Path(cfg["recorder"]["data_dir"]).parent / "alerts.jsonl"
    alerts = []
    if alerts_path.exists():
        for line in alerts_path.read_text().splitlines()[-20:]:
            try:
                a = json.loads(line)
                alerts.append({"ts": a["ts"], "kind": a["kind"], "coin": a["coin"],
                               "net": a["net_edge_bps"] if a["kind"] == "TAKER" else a["maker_net_edge_bps"]})
            except (ValueError, KeyError):
                pass
    kf = Path(cfg["risk"]["kill_switch_file"])
    return {
        "now": time.time(),
        "coin": cfg["paper"]["coin"],
        "state": state,
        "state_age_s": time.time() - state_path.stat().st_mtime if state else None,
        "alerts": alerts[::-1],
        "kill_file": kf.read_text().strip() if kf.exists() else None,
        "market": market,
        "limits": cfg["risk"],
    }


def serve(cfg: dict) -> None:
    d = cfg["dashboard"]
    cache = MarketCache(InfoClient(cfg["network"]["env"]), cfg["paper"]["coin"])

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code: int, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self._send(200, STATIC.read_bytes(), "text/html; charset=utf-8")
            elif self.path == "/api/state":
                body = json.dumps(build_payload(cfg, cache.get())).encode()
                self._send(200, body, "application/json")
            else:
                self._send(404, b"not found", "text/plain")

    srv = ThreadingHTTPServer((d["host"], d["port"]), H)
    log.info("dashboard on http://%s:%d", d["host"], d["port"])
    srv.serve_forever()
