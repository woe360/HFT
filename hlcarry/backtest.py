"""Funding carry backtest on Hyperliquid's own funding history.

Question it answers: "If I had run a simple rule over the last N days, would
carry have made money after costs?"

Rule (per coin, hourly):
  signal = trailing `signal_hours` mean funding rate, annualised
  enter (long spot + short perp) when signal >= enter_apr
  exit when signal <= exit_apr
  every entry+exit pays `round_trip_cost_bps`

Not modelled (on purpose, stated in the output):
  * basis change between entry and exit (no spot price history from the API)
  * slippage beyond the flat cost, partial fills, hedge legging risk
  * Thresholds are swept on the same data they're scored on, so the best row
    is optimistic. Treat the sweep as "does any sane setting work", not "use
    the winner".
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass

from .api import InfoClient

HOURS_PER_YEAR = 8760


def fetch_funding(client: InfoClient, coin: str, days: float, now_ms: int | None = None) -> list[tuple[int, float]]:
    """All hourly funding prints for `coin` over `days`, oldest first. Paginates (API caps each page)."""
    now_ms = now_ms or int(time.time() * 1000)
    start = now_ms - int(days * 86400 * 1000)
    out: dict[int, float] = {}
    while start < now_ms:
        page = client.funding_history(coin, start, now_ms)
        if not page:
            break
        for h in page:
            out[int(h["time"])] = float(h["fundingRate"])
        last = max(int(h["time"]) for h in page)
        if last < start:
            break
        start = last + 1
        if len(page) < 2:
            break
    return sorted(out.items())


@dataclass
class Result:
    coin: str
    enter_apr: float
    exit_apr: float
    hours: int
    hours_in: int
    trades: int
    funding_bps: float
    cost_bps: float
    net_bps: float
    net_apr_on_capital: float
    always_on_net_bps: float   # baseline: enter at start, exit at end

    @property
    def time_in_market(self) -> float:
        return self.hours_in / self.hours if self.hours else 0.0


def simulate(
    coin: str,
    rates: list[float],
    enter_apr: float,
    exit_apr: float,
    round_trip_cost_bps: float,
    signal_hours: int = 24,
    leverage: float = 2.0,
) -> Result:
    window: deque[float] = deque(maxlen=signal_hours)
    in_pos = False
    trades = hours_in = 0
    funding = 0.0
    for r in rates:
        # Decide on information up to the previous hour, then earn this hour's
        # funding if we're in. No look-ahead.
        if len(window) == signal_hours:
            sig = sum(window) / signal_hours * HOURS_PER_YEAR
            if not in_pos and sig >= enter_apr:
                in_pos, trades = True, trades + 1
            elif in_pos and sig <= exit_apr:
                in_pos = False
        if in_pos:
            funding += r
            hours_in += 1
        window.append(r)

    funding_bps = funding * 1e4
    cost_bps = trades * round_trip_cost_bps
    net_bps = funding_bps - cost_bps
    hours = len(rates)
    capital = 1 + 1 / leverage
    net_apr = (net_bps / 1e4) / capital * (HOURS_PER_YEAR / hours) if hours else 0.0
    always = sum(rates) * 1e4 - round_trip_cost_bps
    return Result(coin, enter_apr, exit_apr, hours, hours_in, trades,
                  funding_bps, cost_bps, net_bps, net_apr, always)


def sweep(coin: str, rates: list[float], cost_bps: float, signal_hours: int, leverage: float,
          enters=(0.12, 0.15, 0.20, 0.30, 0.50), exit_ratio: float = 0.5) -> list[Result]:
    """Try a few entry thresholds; exit when the signal falls to `exit_ratio` x entry."""
    return [simulate(coin, rates, e, e * exit_ratio, cost_bps, signal_hours, leverage) for e in enters]


def run(cfg: dict, coins: list[str], days: float, maker: bool = False,
        client: InfoClient | None = None) -> dict[str, list[Result]]:
    client = client or InfoClient(cfg["network"]["env"])
    f, bt = cfg["fees"], cfg.get("backtest", {})
    if maker:
        cost = 2 * (f["spot_maker"] + f["perp_maker"]) * 1e4
    else:
        cost = 2 * (f["spot_taker"] + f["perp_taker"]) * 1e4 + bt.get("taker_slippage_bps", 4.0)
    out = {}
    for c in coins:
        rates = [r for _, r in fetch_funding(client, c, days)]
        if len(rates) < bt.get("signal_hours", 24) + 24:
            continue
        out[c] = sweep(c, rates, cost, bt.get("signal_hours", 24), cfg["risk"]["max_leverage"])
    return out


def format_results(res: dict[str, list[Result]], days: float, maker: bool) -> str:
    lines = [f"Funding carry backtest, last {days:.0f} days, {'maker' if maker else 'taker'} costs",
             f"{'coin':<8}{'enter':>7}{'exit':>7}{'trades':>8}{'in mkt':>8}{'funding':>9}{'cost':>8}{'NET':>8}{'APR cap':>9}{'always-on':>11}"]
    lines.append("-" * len(lines[-1]))
    for coin, rows in res.items():
        for r in rows:
            lines.append(
                f"{coin:<8}{r.enter_apr:>7.0%}{r.exit_apr:>7.0%}{r.trades:>8}{r.time_in_market:>8.0%}"
                f"{r.funding_bps:>9.1f}{r.cost_bps:>8.1f}{r.net_bps:>8.1f}{r.net_apr_on_capital:>9.1%}{r.always_on_net_bps:>11.1f}"
            )
    lines += [
        "bps of notional per leg. enter/exit = trailing 24h funding, annualised. APR cap = on spot capital + perp margin.",
        "always-on = hold the whole period, one round trip. Not modelled: basis P&L, legging risk, real slippage.",
        "Thresholds are scored on the same data they're picked from: the best row is optimistic.",
    ]
    return "\n".join(lines)
