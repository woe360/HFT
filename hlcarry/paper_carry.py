"""Paper "hold the carry" loop.

Holds long spot + short perp on one coin (default HYPE) on paper, using live
Hyperliquid books for fills and Hyperliquid's real hourly funding prints for
income. Every order goes through the risk layer; the kill switch can stop and
flatten it. State is saved to data/paper/state.json after every tick, so the
loop survives restarts and the dashboard can read it.

Read-only against the exchange: no keys, no signing.

Rules (from the 180-day backtest, where holding beat timing):
  * flat and trailing funding >= exit_apr + 5% APR  -> enter
  * in carry and trailing funding <= exit_apr       -> exit
  * kill switch / risk trip                          -> flatten, stay out
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from .api import InfoClient
from .costs import mid
from .paper import PaperExecutor
from .risk import Book, KillSwitch, Limits, Order, RiskEngine
from .scanner import match_markets

log = logging.getLogger("paper_carry")
REENTER_BUFFER_APR = 0.05
MAX_EQUITY_POINTS = 20_000
MAX_EVENTS = 300


def _k(market: str, coin: str) -> str:
    return f"{market}|{coin}"


class PaperCarry:
    def __init__(self, cfg: dict, client: InfoClient | None = None, clock=time.time):
        self.cfg = cfg
        p = cfg["paper"]
        self.coin = p["coin"]
        self.notional = p["notional_usd"]
        self.exit_apr = p["exit_funding_apr"]
        self.lookback_h = p["exit_lookback_hours"]
        self.tick_s = p["tick_seconds"]
        self.state_path = Path(p["state_dir"]) / "state.json"
        self.client = client or InfoClient(cfg["network"]["env"])
        self.clock = clock

        self.state = self._load() or self._fresh(p["starting_equity_usd"])
        self.book = Book(equity_usd=self.state["cash"], day_start_equity_usd=self.state["day_start_equity"])
        for k, q in self.state["positions"].items():
            m, c = k.split("|", 1)
            self.book.positions[(m, c)] = q
            self.book.prices[(m, c)] = self.state["prices"].get(k, 0.0)
        self.ex = PaperExecutor(self.book, cfg["fees"])
        self.ex.cash = self.state["cash"]
        self.ex.mark()
        self.kill = KillSwitch(cfg["risk"]["kill_switch_file"], self.ex)
        self.risk = RiskEngine(Limits.from_cfg(cfg), self.book, self.kill)
        self.ex.risk = self.risk

    # ---------- state ----------
    def _fresh(self, equity: float) -> dict:
        now = self.clock()
        return {
            "version": 1, "coin": self.coin, "spot_coin": None, "started_at": now,
            "status": "flat", "cash": equity, "starting_equity": equity,
            "day_start_equity": equity, "day": time.strftime("%Y-%m-%d", time.gmtime(now)),
            "positions": {}, "prices": {}, "entry": None,
            "fees_paid": 0.0, "funding_earned": 0.0, "funding": [],
            "last_funding_ms": int(now * 1000), "equity": [], "events": [],
            "market": {}, "risk": {},
        }

    def _load(self) -> dict | None:
        try:
            return json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return None

    def save(self) -> None:
        s = self.state
        s["cash"] = self.ex.cash
        s["positions"] = {_k(*k): q for k, q in self.book.positions.items() if q}
        s["prices"] = {_k(*k): px for k, px in self.book.prices.items()}
        s["equity"] = s["equity"][-MAX_EQUITY_POINTS:]
        s["events"] = s["events"][-MAX_EVENTS:]
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(s, separators=(",", ":")))
        tmp.replace(self.state_path)  # atomic: the dashboard never reads half a file

    def event(self, msg: str) -> None:
        log.info(msg)
        self.state["events"].append({"ts": self.clock(), "msg": msg})

    # ---------- market data ----------
    def _spot_coin(self) -> str:
        if not self.state.get("spot_coin"):
            ms = match_markets(self.client.perp_meta_and_ctxs(), self.client.spot_meta_and_ctxs(),
                               self.cfg["scanner"].get("spot_aliases", {}))
            m = next((m for m in ms if m.coin == self.coin), None)
            if m is None:
                raise RuntimeError(f"no USDC spot market found for {self.coin}")
            self.state["spot_coin"] = m.spot_coin
        return self.state["spot_coin"]

    def _perp_ctx(self) -> dict:
        meta, ctxs = self.client.perp_meta_and_ctxs()
        for a, c in zip(meta["universe"], ctxs):
            if a["name"] == self.coin:
                return c
        raise RuntimeError(f"{self.coin} not in perp universe")

    # ---------- the loop ----------
    def tick(self) -> None:
        now = self.clock()
        spot_coin = self._spot_coin()
        ctx = self._perp_ctx()
        pbook, sbook = self.client.l2_book(self.coin), self.client.l2_book(spot_coin)
        pmid, smid = mid(pbook), mid(sbook)
        self.book.last_data_ts = time.monotonic()
        self.ex.mark({("perp", self.coin): pmid, ("spot", spot_coin): smid})

        # New UTC day: reset the daily loss baseline.
        day = time.strftime("%Y-%m-%d", time.gmtime(now))
        if day != self.state["day"]:
            self.state["day"], self.book.day_start_equity_usd = day, self.book.equity_usd
            self.state["day_start_equity"] = self.book.equity_usd

        self._apply_new_funding(now, float(ctx["markPx"]))

        hist = self.client.funding_history(self.coin, int((now - self.lookback_h * 3600) * 1000))
        rates = [float(h["fundingRate"]) for h in hist]
        trailing_apr = sum(rates) / len(rates) * 8760 if rates else 0.0

        health = self.risk.check_health()
        if not health.ok and self.state["status"] != "killed":
            self.state["status"] = "killed"
            self.event(f"stopped by risk layer: {health.reason}")

        status = self.state["status"]
        if status == "flat" and trailing_apr >= self.exit_apr + REENTER_BUFFER_APR:
            self._enter(spot_coin, pbook, sbook, trailing_apr)
        elif status == "in_carry":
            if trailing_apr <= self.exit_apr:
                self.event(f"exit: trailing funding {trailing_apr:.1%} APR <= {self.exit_apr:.1%}")
                self._exit(pbook, sbook)
            else:
                hedge = self.risk.check_hedges([(self.coin, spot_coin)])
                if not hedge.ok:
                    self.kill.trip(hedge.reason)
                    self.state["status"] = "killed"
                    self.event(f"kill switch: {hedge.reason}")

        self.ex.mark()
        self.state["market"] = {
            "ts": now, "mark": float(ctx["markPx"]), "perp_mid": pmid, "spot_mid": smid,
            "basis_bps": (pmid - smid) / smid * 1e4,
            "funding_now_apr": float(ctx["funding"]) * 8760, "trailing_apr": trailing_apr,
            "open_interest": float(ctx.get("openInterest", 0) or 0),
        }
        self.state["risk"] = {
            "kill_tripped": self.kill.tripped, "kill_reason": self.kill.reason,
            "hedge_imbalance": self.risk.hedge_imbalance(self.coin, spot_coin),
            "daily_pnl": self.book.daily_pnl(), "gross_notional": self.book.gross(),
            "leverage": self.book.gross() / self.book.equity_usd if self.book.equity_usd else 0.0,
        }
        eq = self.state["equity"]
        if not eq or now - eq[-1][0] >= 300 or abs(self.book.equity_usd - eq[-1][1]) > 0.5:
            eq.append([now, round(self.book.equity_usd, 4)])
        self.save()

    def _apply_new_funding(self, now: float, mark: float) -> None:
        since = self.state["last_funding_ms"]
        prints = [h for h in self.client.funding_history(self.coin, since + 1) if int(h["time"]) > since]
        for h in sorted(prints, key=lambda h: int(h["time"])):
            rate, t = float(h["fundingRate"]), int(h["time"])
            # GUESS: uses the current mark for the payment; Hyperliquid uses the oracle
            # price at the funding time. Difference is tiny for hourly prints.
            paid = self.ex.apply_funding(self.coin, rate, mark)
            self.state["last_funding_ms"] = t
            if paid:
                self.state["funding_earned"] += paid
                self.state["funding"].append({"ts": t / 1000, "rate": rate, "paid": paid})

    def _enter(self, spot_coin: str, pbook: dict, sbook: dict, trailing_apr: float) -> None:
        cash0 = self.ex.cash
        spx, ppx = mid(sbook), mid(pbook)
        r1 = self.ex.submit(Order(spot_coin, "spot", "buy", self.notional / spx, spx, hedge_of=self.coin), sbook)
        if r1["status"] != "filled":
            self.event(f"entry blocked: {r1['reason']}")
            return
        r2 = self.ex.submit(Order(self.coin, "perp", "sell", self.notional / ppx, ppx), pbook)
        if r2["status"] != "filled":
            # Never sit on a naked leg: undo the spot buy straight away.
            self.event(f"perp leg rejected ({r2['reason']}); unwinding spot leg")
            self.ex.flatten()
            return
        fees = r1["fee"] + r2["fee"]
        self.state["fees_paid"] += fees
        self.state["status"] = "in_carry"
        self.state["entry"] = {"ts": self.clock(), "spot_px": r1["avg_px"], "perp_px": r2["avg_px"],
                               "spot_qty": r1["qty"], "perp_qty": r2["qty"], "fees": fees,
                               "trailing_apr": trailing_apr, "cash_before": cash0}
        self.event(f"enter carry {self.coin}: long {r1['qty']:.4f} spot @ {r1['avg_px']:.4f}, "
                   f"short {r2['qty']:.4f} perp @ {r2['avg_px']:.4f}, fees ${fees:.2f}, "
                   f"trailing funding {trailing_apr:.1%} APR")

    def _exit(self, pbook: dict, sbook: dict) -> None:
        before = self.ex.cash
        fee_rate = self.cfg["fees"]
        gross = self.book.gross()
        self.ex.flatten()
        est_fees = gross * (fee_rate["spot_taker"] + fee_rate["perp_taker"]) / 2
        self.state["fees_paid"] += est_fees
        self.state["status"] = "flat"
        self.state["entry"] = None
        self.event(f"exited carry, cash {before:.2f} -> {self.ex.cash:.2f}")

    def run_forever(self) -> None:
        self.event(f"paper carry loop started on {self.coin}, ${self.notional} per leg, status {self.state['status']}")
        self.save()
        while True:
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001 - keep running through network blips
                log.exception("tick failed")
                self.event(f"tick failed: {e!r}")
                self.save()
            time.sleep(self.tick_s)
