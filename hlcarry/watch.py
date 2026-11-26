"""Watcher: re-run the scanner on a timer and alert when a carry trade clears costs.

Alerts go to a macOS notification (when on a Mac), the log, and
data/alerts.jsonl. Each coin has a cooldown so one hot market doesn't spam.
Read-only: it never trades.
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
import time
from pathlib import Path

from .scanner import Opportunity, scan

log = logging.getLogger("watch")


def notify(title: str, body: str) -> None:
    log.warning("ALERT %s: %s", title, body)
    if sys.platform == "darwin":
        script = f"display notification {json.dumps(body)} with title {json.dumps(title)} sound name \"Glass\""
        try:
            subprocess.run(["osascript", "-e", script], check=False, timeout=5)
        except Exception:  # noqa: BLE001 - an alert failing must never stop the loop
            log.exception("osascript failed")


def classify(o: Opportunity, min_bps: float, max_cost_bps: float = 60.0) -> str | None:
    # Thin markets: a maker edge there is fantasy, since getting out means crossing a wide book.
    if not o.books_deep_enough or o.fee_bps + o.slippage_bps > max_cost_bps:
        return None
    if o.net_edge_bps >= min_bps:
        return "TAKER"   # clears costs even crossing the spread: actionable
    if o.maker_net_edge_bps >= min_bps:
        return "MAKER"   # only with patient limit orders: worth a look
    return None


class Watcher:
    def __init__(self, cfg: dict, scan_fn=scan, notify_fn=notify, clock=time.time):
        w = cfg.get("watch", {})
        self.cfg = cfg
        self.every = w.get("interval_minutes", 15) * 60
        self.cooldown = w.get("cooldown_hours", 6) * 3600
        self.min_bps = cfg["scanner"]["min_net_edge_bps"]
        self.max_cost_bps = w.get("max_round_trip_cost_bps", 60)
        self.alerts_path = Path(cfg["recorder"]["data_dir"]).parent / "alerts.jsonl"
        self.scan_fn, self.notify_fn, self.clock = scan_fn, notify_fn, clock
        self.last_alert: dict[tuple[str, str], float] = {}

    def tick(self) -> list[tuple[str, Opportunity]]:
        fired = []
        for o in self.scan_fn(self.cfg):
            kind = classify(o, self.min_bps, self.max_cost_bps)
            if not kind:
                continue
            key, now = (o.coin, kind), self.clock()
            if now - self.last_alert.get(key, -1e18) < self.cooldown:
                continue
            self.last_alert[key] = now
            days = self.cfg["scanner"]["hold_hours"] / 24
            edge = o.net_edge_bps if kind == "TAKER" else o.maker_net_edge_bps
            self.notify_fn(
                f"hl-carry {kind}: {o.coin}",
                f"{edge:+.1f} bps net over {days:.0f}d | funding avg {o.funding_mean_apr:.0%} APR, now {o.funding_now_apr:.0%}",
            )
            self.alerts_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.alerts_path, "a") as f:
                f.write(json.dumps({"ts": now, "kind": kind, **o.as_dict()}) + "\n")
            fired.append((kind, o))
        return fired

    def run_forever(self) -> None:
        log.info("watching every %.0f min, cooldown %.0f h, min edge %s bps",
                 self.every / 60, self.cooldown / 3600, self.min_bps)
        while True:
            try:
                fired = self.tick()
                log.info("scan done, %d alert(s)", len(fired))
            except Exception:  # noqa: BLE001 - network blips must not kill the watcher
                log.exception("scan failed; retrying next interval")
            time.sleep(self.every)
