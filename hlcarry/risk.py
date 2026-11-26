"""Risk layer: every order passes through here before any executor sees it.

Design rules:
  * Fail closed. Any missing data, stale feed or tripped switch => reject.
  * The kill switch is sticky. Once tripped it stays tripped until a human
    deletes the KILL file and restarts the process.
  * Tripping cancels all orders and asks the executor to flatten.
  * Reducing orders (that shrink exposure) are still allowed while the
    switch is off but limits are breached, so we can always get out.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

log = logging.getLogger("risk")


@dataclass(frozen=True)
class Limits:
    max_notional_per_coin_usd: float
    max_gross_notional_usd: float
    max_leverage: float
    max_hedge_imbalance: float
    max_daily_loss_usd: float
    max_orders_per_minute: int
    kill_switch_file: str = "KILL"
    stale_after_seconds: float = 20.0

    @classmethod
    def from_cfg(cls, cfg: dict) -> "Limits":
        r = cfg["risk"]
        return cls(
            max_notional_per_coin_usd=r["max_notional_per_coin_usd"],
            max_gross_notional_usd=r["max_gross_notional_usd"],
            max_leverage=r["max_leverage"],
            max_hedge_imbalance=r["max_hedge_imbalance"],
            max_daily_loss_usd=r["max_daily_loss_usd"],
            max_orders_per_minute=r["max_orders_per_minute"],
            kill_switch_file=r["kill_switch_file"],
            stale_after_seconds=cfg["recorder"]["stale_after_seconds"],
        )


@dataclass(frozen=True)
class Order:
    coin: str           # "ETH" for the perp, spot book name for spot
    market: str         # "perp" or "spot"
    side: str           # "buy" or "sell"
    qty: float
    px: float           # limit price (or reference price for a taker order)
    hedge_of: str | None = None  # perp coin this spot leg hedges

    @property
    def notional(self) -> float:
        return abs(self.qty * self.px)

    @property
    def signed_qty(self) -> float:
        return self.qty if self.side == "buy" else -self.qty


@dataclass
class Book:
    """Our positions and PnL as the risk engine sees them."""
    positions: dict[tuple[str, str], float] = field(default_factory=dict)  # (market, coin) -> qty
    prices: dict[tuple[str, str], float] = field(default_factory=dict)
    equity_usd: float = 0.0
    day_start_equity_usd: float = 0.0
    last_data_ts: float = field(default_factory=time.monotonic)

    def notional(self, market: str, coin: str) -> float:
        k = (market, coin)
        return abs(self.positions.get(k, 0.0) * self.prices.get(k, 0.0))

    def gross(self) -> float:
        return sum(abs(q * self.prices.get(k, 0.0)) for k, q in self.positions.items())

    def daily_pnl(self) -> float:
        return self.equity_usd - self.day_start_equity_usd


@dataclass(frozen=True)
class Decision:
    ok: bool
    reason: str = ""


class Executor(Protocol):
    def cancel_all(self) -> None: ...
    def flatten(self) -> None: ...


class KillSwitch:
    def __init__(self, path: str | Path, executor: Executor | None = None):
        self.path = Path(path)
        self.executor = executor
        self._tripped = False
        self.reason = ""

    @property
    def tripped(self) -> bool:
        if not self._tripped and self.path.exists():
            self.trip(f"kill file present: {self.path}")
        return self._tripped

    def trip(self, reason: str) -> None:
        if self._tripped:
            return
        self._tripped = True
        self.reason = reason
        log.critical("KILL SWITCH TRIPPED: %s", reason)
        try:
            self.path.write_text(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {reason}\n")
        except OSError:
            log.exception("could not write kill file")
        if self.executor:
            # Cancel first so no resting order fills while we flatten.
            try:
                self.executor.cancel_all()
            finally:
                self.executor.flatten()


class RiskEngine:
    def __init__(self, limits: Limits, book: Book, kill: KillSwitch, clock=time.monotonic):
        self.l = limits
        self.book = book
        self.kill = kill
        self.clock = clock
        self._order_times: deque[float] = deque()

    # -- checks that can trip the switch, run on every tick and every order --
    def check_health(self) -> Decision:
        if self.kill.tripped:
            return Decision(False, f"kill switch: {self.kill.reason}")
        if self.book.daily_pnl() <= -self.l.max_daily_loss_usd:
            self.kill.trip(f"daily loss {self.book.daily_pnl():.2f} <= -{self.l.max_daily_loss_usd}")
            return Decision(False, "daily loss limit")
        if self.book.equity_usd > 0 and self.book.gross() / self.book.equity_usd > self.l.max_leverage * 1.5:
            self.kill.trip(f"leverage {self.book.gross() / self.book.equity_usd:.2f}x far over limit")
            return Decision(False, "leverage way over limit")
        return Decision(True)

    def _is_reducing(self, o: Order) -> bool:
        cur = self.book.positions.get((o.market, o.coin), 0.0)
        return cur != 0 and (cur > 0) != (o.signed_qty > 0) and abs(o.signed_qty) <= abs(cur)

    def check_order(self, o: Order) -> Decision:
        h = self.check_health()
        if not h.ok:
            return h
        now = self.clock()
        if now - self.book.last_data_ts > self.l.stale_after_seconds:
            return Decision(False, "market data stale")
        if o.qty <= 0 or o.px <= 0:
            return Decision(False, "bad qty/px")

        while self._order_times and now - self._order_times[0] > 60:
            self._order_times.popleft()
        if len(self._order_times) >= self.l.max_orders_per_minute:
            return Decision(False, "order rate limit")

        if not self._is_reducing(o):
            k = (o.market, o.coin)
            new_qty = self.book.positions.get(k, 0.0) + o.signed_qty
            if abs(new_qty * o.px) > self.l.max_notional_per_coin_usd:
                return Decision(False, f"per-coin notional > {self.l.max_notional_per_coin_usd}")
            if self.book.gross() + o.notional > self.l.max_gross_notional_usd:
                return Decision(False, f"gross notional > {self.l.max_gross_notional_usd}")
            eq = self.book.equity_usd
            if eq <= 0 or (self.book.gross() + o.notional) / eq > self.l.max_leverage:
                return Decision(False, f"leverage > {self.l.max_leverage}x")

        self._order_times.append(now)
        return Decision(True)

    def hedge_imbalance(self, perp_coin: str, spot_coin: str) -> float:
        perp = self.book.positions.get(("perp", perp_coin), 0.0)
        spot = self.book.positions.get(("spot", spot_coin), 0.0)
        if perp == 0:
            return 0.0 if spot == 0 else float("inf")
        # Carry is long spot + short perp, so spot + perp should be ~0.
        return abs(spot + perp) / abs(perp)

    def check_hedges(self, pairs: list[tuple[str, str]]) -> Decision:
        """Call after fills settle. A broken hedge is a directional bet we did not choose."""
        for perp_coin, spot_coin in pairs:
            imb = self.hedge_imbalance(perp_coin, spot_coin)
            if imb > self.l.max_hedge_imbalance:
                return Decision(False, f"hedge imbalance {perp_coin}: {imb:.1%}")
        return Decision(True)
