# hl-carry

A small-scale version of a professional market-neutral setup on Hyperliquid.
Read-only and paper only. There is no code path that signs or sends a real order.

## What's here

| Module | Job |
|---|---|
| `hlcarry/recorder.py` | Streams `l2Book`, `trades`, `activeAssetCtx` over WebSocket for a few coins, polls REST asset contexts every minute as a cross-check, writes everything raw to `data/raw/<stream>/<date>/<hour>.jsonl.gz` with a local receive timestamp. Reconnects with backoff and logs every connect, drop and stale feed to `_events`, so gaps are visible. |
| `hlcarry/scanner.py` | Ranks cash-and-carry trades (buy spot, short perp, collect funding) by **net edge after fees and slippage**, using the funding average over the lookback and live order books walked at your trade size. |
| `hlcarry/risk.py` | Limits (per-coin notional, gross notional, leverage, daily loss, order rate, stale data, hedge imbalance) and a sticky kill switch that cancels and flattens. Fails closed. |
| `hlcarry/paper.py` | Paper executor that fills against a book snapshot, charges taker fees and applies hourly funding, so the risk layer can be tested end to end. |

## Run it

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

python -m hlcarry scan                 # rank carry opportunities now
python -m hlcarry scan --coins ETH,BTC --json
python -m hlcarry record               # record until Ctrl-C (run under systemd/tmux on a VPS)
python -m hlcarry status               # kill switch state + data on disk
python -m hlcarry kill "reason"        # trip the kill switch; delete ./KILL to re-arm
pytest -q                              # 11 tests, no network needed
```

Override anything in `config/default.toml` with `config/local.toml` (git-ignored).

## How to read the scanner

All numbers are bps of notional per leg over `hold_hours` (default 1 week):

* **carry** = mean hourly funding over the lookback x hold hours
* **fees** = 4 taker fills (enter and exit, spot and perp). About 23 bps at base tier.
* **slip** = walking today's books at `notional_usd`, both legs, both ways
* **NET** = carry - fees - slip. **APR cap** = NET annualised on the capital it ties up (full spot + perp margin).

Rule of thumb from the fee math: at base-tier taker fees a carry needs roughly
**12%+ APR funding sustained for a week** just to break even. Holding longer, or
entering with maker orders, is what makes it pay. Flags: `below-min`,
`thin-book`, `funding-unstable` (funding positive in under 70% of lookback hours).

## Guesses to verify (marked in config)

* Fee rates are the base tier as I remember them. Check the Hyperliquid fee page.
* Spot names for BTC/ETH/SOL are assumed to be `UBTC`/`UETH`/`USOL`. Check `spotMeta`.
* Positive funding only: shorting spot needs borrowing, which v1 doesn't do.

## Next milestones

1. Run `record` on a VPS for 48h with zero unexplained gaps in `_events`.
2. Backtest from recorded data: entry/exit rules, maker vs taker entry, funding flips.
3. Paper loop: scanner -> risk -> paper executor on live data for 2+ weeks.
4. Testnet orders with a trade-only API wallet (Hyperliquid "agent" key, cannot withdraw).
5. Tiny mainnet size. Only then think about a market maker, and only then Rust.

## Security rules

* No keys in this repo, ever. `.gitignore` blocks `.env`, `*.key`, `config/local.toml`.
* When orders arrive (milestone 4) use a Hyperliquid API/agent wallet: it can trade but not withdraw.
* The kill switch file is checked on every order, so ops can stop it from a shell without the code cooperating.
