"""Paper executor: fills taker orders against a book snapshot, charges fees.

It exists so the risk layer and (later) strategy loop can be exercised end
to end without any keys. It cannot talk to the exchange.
"""
from __future__ import annotations

import logging

from .costs import walk_book
from .risk import Book, Order, RiskEngine

log = logging.getLogger("paper")


class PaperExecutor:
    def __init__(self, book: Book, fees: dict):
        self.book = book
        self.fees = fees
        self.cash = book.equity_usd
        self.fills: list[dict] = []
        self.risk: RiskEngine | None = None

    def submit(self, o: Order, l2: dict) -> dict:
        if self.risk:
            d = self.risk.check_order(o)
            if not d.ok:
                log.warning("REJECT %s: %s", o, d.reason)
                return {"status": "rejected", "reason": d.reason}
        bids, asks = l2["levels"]
        fill = walk_book(asks if o.side == "buy" else bids, o.notional)
        if not fill.complete:
            return {"status": "rejected", "reason": "book too thin"}
        fee = fill.filled_notional * self.fees[f"{o.market}_taker"]
        qty = fill.filled_notional / fill.avg_px
        signed = qty if o.side == "buy" else -qty
        k = (o.market, o.coin)
        self.book.positions[k] = self.book.positions.get(k, 0.0) + signed
        self.book.prices[k] = fill.avg_px
        self.cash -= signed * fill.avg_px + fee
        rec = {"status": "filled", "order": o, "avg_px": fill.avg_px, "qty": qty, "fee": fee}
        self.fills.append(rec)
        self.mark()
        return rec

    def apply_funding(self, perp_coin: str, rate: float, mark_px: float) -> float:
        """Hourly funding: positive rate means longs pay shorts."""
        pos = self.book.positions.get(("perp", perp_coin), 0.0)
        pay = -pos * mark_px * rate
        self.cash += pay
        self.mark()
        return pay

    def mark(self, prices: dict | None = None) -> None:
        if prices:
            self.book.prices.update(prices)
        # Perp PnL is cash-settled here for simplicity; spot is held at mark.
        self.book.equity_usd = self.cash + sum(
            q * self.book.prices.get(k, 0.0) for k, q in self.book.positions.items()
        )

    def cancel_all(self) -> None:
        log.warning("paper: cancel_all (no resting orders in paper v1)")

    def flatten(self) -> None:
        log.warning("paper: flatten %s", self.book.positions)
        for k, q in list(self.book.positions.items()):
            px = self.book.prices.get(k, 0.0)
            self.cash += q * px - abs(q * px) * self.fees[f"{k[0]}_taker"]
            self.book.positions[k] = 0.0
        self.mark()
