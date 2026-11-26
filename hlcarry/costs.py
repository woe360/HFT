"""Execution cost model: fees plus slippage from walking the order book."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Fill:
    avg_px: float
    filled_notional: float
    complete: bool  # False if the book was too thin for the requested size


def walk_book(levels: list[dict], notional_usd: float) -> Fill:
    """Average fill price for a market order of `notional_usd` against one side.

    `levels` is one side of an l2Book: [{"px": "...", "sz": "..."}...], best first.
    """
    remaining = notional_usd
    qty = 0.0
    spent = 0.0
    for lvl in levels:
        px, sz = float(lvl["px"]), float(lvl["sz"])
        take = min(remaining, px * sz)
        qty += take / px
        spent += take
        remaining -= take
        if remaining <= 1e-9:
            break
    if qty == 0:
        return Fill(float("nan"), 0.0, False)
    return Fill(spent / qty, spent, remaining <= 1e-9)


def mid(book: dict) -> float:
    bids, asks = book["levels"]
    return (float(bids[0]["px"]) + float(asks[0]["px"])) / 2


def slippage_bps(book: dict, side: str, notional_usd: float) -> tuple[float, bool]:
    """Cost vs mid, in bps, of a taker order. side is 'buy' or 'sell'."""
    bids, asks = book["levels"]
    m = mid(book)
    fill = walk_book(asks if side == "buy" else bids, notional_usd)
    if not fill.filled_notional:
        return float("inf"), False
    diff = fill.avg_px - m if side == "buy" else m - fill.avg_px
    return diff / m * 1e4, fill.complete
