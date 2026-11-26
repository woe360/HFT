"""Funding carry scanner.

Trade idea (cash-and-carry): when perp funding is persistently positive, longs
pay shorts. Buy spot and short the same notional of perp. Price moves cancel;
we collect funding. Hyperliquid funding is paid every hour.

The scanner only reports. It never places orders. For each coin that has both
a perp and a USDC spot market it estimates, for `notional_usd` per leg:

  expected carry  = mean hourly funding (lookback) * hold_hours
  costs           = taker fees on 4 fills (enter + exit, both legs)
                    + slippage from walking the current books, both legs, both ways
  net edge        = carry - costs                    (bps of notional)

Limitations (on purpose, v1):
  * Positive funding only. The reverse (short spot) needs borrowing, which
    Hyperliquid spot does not offer.
  * Exit slippage is assumed equal to what the book shows now.
  * Basis change between entry and exit is reported, not forecast.
"""
from __future__ import annotations

import statistics
import time
from dataclasses import asdict, dataclass

from .api import InfoClient
from .costs import mid, slippage_bps

USDC_TOKEN_INDEX = 0


@dataclass
class Market:
    coin: str            # perp name, e.g. "ETH"
    spot_coin: str       # spot book name, e.g. "@151" or "PURR/USDC"
    funding_now: float   # current predicted hourly funding rate
    mark_px: float
    day_ntl_vlm: float


@dataclass
class Opportunity:
    coin: str
    spot_coin: str
    funding_now_apr: float
    funding_mean_apr: float
    funding_pos_share: float   # share of lookback hours with positive funding
    basis_bps: float           # (perp mid - spot mid) / spot mid
    carry_bps: float
    fee_bps: float
    slippage_bps: float
    net_edge_bps: float
    net_apr_on_capital: float  # after costs, on spot capital + perp margin
    books_deep_enough: bool
    perp_day_volume_usd: float

    def as_dict(self):
        return asdict(self)


def match_markets(perp_payload, spot_payload, aliases: dict[str, str]) -> list[Market]:
    perp_meta, perp_ctxs = perp_payload
    spot_meta, _spot_ctxs = spot_payload
    token_by_name = {t["name"]: t["index"] for t in spot_meta["tokens"]}
    spot_by_base = {
        p["tokens"][0]: p["name"]
        for p in spot_meta["universe"]
        if p["tokens"][1] == USDC_TOKEN_INDEX
    }
    out = []
    for asset, ctx in zip(perp_meta["universe"], perp_ctxs):
        if asset.get("isDelisted"):
            continue
        name = asset["name"]
        token_idx = token_by_name.get(aliases.get(name, name))
        if token_idx is None or token_idx not in spot_by_base:
            continue
        out.append(Market(
            coin=name,
            spot_coin=spot_by_base[token_idx],
            funding_now=float(ctx["funding"]),
            mark_px=float(ctx["markPx"]),
            day_ntl_vlm=float(ctx.get("dayNtlVlm", 0) or 0),
        ))
    return out


def evaluate(
    m: Market,
    funding_hist: list[dict],
    perp_book: dict,
    spot_book: dict,
    fees: dict,
    notional_usd: float,
    hold_hours: float,
    leverage: float,
) -> Opportunity:
    rates = [float(h["fundingRate"]) for h in funding_hist] or [m.funding_now]
    mean_rate = statistics.fmean(rates)
    pos_share = sum(r > 0 for r in rates) / len(rates)

    carry_bps = mean_rate * hold_hours * 1e4
    fee_bps = 2 * (fees["spot_taker"] + fees["perp_taker"]) * 1e4

    s_buy, ok1 = slippage_bps(spot_book, "buy", notional_usd)    # enter: buy spot
    p_sell, ok2 = slippage_bps(perp_book, "sell", notional_usd)  # enter: short perp
    s_sell, ok3 = slippage_bps(spot_book, "sell", notional_usd)  # exit: sell spot
    p_buy, ok4 = slippage_bps(perp_book, "buy", notional_usd)    # exit: buy back perp
    slip_bps = s_buy + p_sell + s_sell + p_buy

    net_bps = carry_bps - fee_bps - slip_bps
    # Capital tied up: full spot notional + perp margin at `leverage`.
    capital_per_notional = 1 + 1 / leverage
    net_apr = (net_bps / 1e4) / capital_per_notional * (8760 / hold_hours)

    pm, sm = mid(perp_book), mid(spot_book)
    return Opportunity(
        coin=m.coin,
        spot_coin=m.spot_coin,
        funding_now_apr=m.funding_now * 8760,
        funding_mean_apr=mean_rate * 8760,
        funding_pos_share=pos_share,
        basis_bps=(pm - sm) / sm * 1e4,
        carry_bps=carry_bps,
        fee_bps=fee_bps,
        slippage_bps=slip_bps,
        net_edge_bps=net_bps,
        net_apr_on_capital=net_apr,
        books_deep_enough=all((ok1, ok2, ok3, ok4)),
        perp_day_volume_usd=m.day_ntl_vlm,
    )


def scan(cfg: dict, client: InfoClient | None = None, coins: list[str] | None = None) -> list[Opportunity]:
    sc, fees = cfg["scanner"], cfg["fees"]
    client = client or InfoClient(cfg["network"]["env"])
    markets = match_markets(
        client.perp_meta_and_ctxs(), client.spot_meta_and_ctxs(), sc.get("spot_aliases", {})
    )
    if coins:
        markets = [m for m in markets if m.coin in coins]
    start_ms = int((time.time() - sc["lookback_hours"] * 3600) * 1000)
    results = []
    for m in markets:
        hist = client.funding_history(m.coin, start_ms)
        if not hist and m.funding_now <= 0:
            continue
        mean = statistics.fmean(float(h["fundingRate"]) for h in hist) if hist else m.funding_now
        if mean <= 0:
            continue  # v1 only does long-spot / short-perp
        opp = evaluate(
            m, hist, client.l2_book(m.coin), client.l2_book(m.spot_coin), fees,
            sc["notional_usd"], sc["hold_hours"], cfg["risk"]["max_leverage"],
        )
        if abs(opp.basis_bps) > sc.get("max_abs_basis_bps", 300):
            continue  # not the same asset: name collision or price scaling
        results.append(opp)
    results.sort(key=lambda o: o.net_edge_bps, reverse=True)
    return results


def format_table(opps: list[Opportunity], min_edge_bps: float) -> str:
    hdr = f"{'coin':<8}{'fund now':>10}{'fund avg':>10}{'pos%':>6}{'basis':>8}{'carry':>8}{'fees':>7}{'slip':>7}{'NET':>8}{'APR cap':>9}  flags"
    lines = [hdr, "-" * len(hdr)]
    for o in opps:
        flags = []
        if o.net_edge_bps < min_edge_bps:
            flags.append("below-min")
        if not o.books_deep_enough:
            flags.append("thin-book")
        if o.funding_pos_share < 0.7:
            flags.append("funding-unstable")
        lines.append(
            f"{o.coin:<8}{o.funding_now_apr:>9.1%} {o.funding_mean_apr:>9.1%} {o.funding_pos_share:>5.0%}"
            f"{o.basis_bps:>8.1f}{o.carry_bps:>8.1f}{o.fee_bps:>7.1f}{o.slippage_bps:>7.1f}"
            f"{o.net_edge_bps:>8.1f}{o.net_apr_on_capital:>8.1%}   {','.join(flags)}"
        )
    lines.append("bps of notional per leg over the hold period. APR cap = net return on spot capital + perp margin.")
    return "\n".join(lines)
