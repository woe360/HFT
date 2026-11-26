"""CLI.

  python -m hlcarry scan [--coins ETH,BTC] [--json]
  python -m hlcarry record
  python -m hlcarry watch               # alert when a carry trade clears costs
  python -m hlcarry backtest [--days 180] [--coins HYPE,BTC] [--maker]
  python -m hlcarry kill "reason"       # trip the kill switch from a shell
  python -m hlcarry status              # show kill switch state + data on disk
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
from pathlib import Path

from .config import load_config


def main(argv=None):
    ap = argparse.ArgumentParser(prog="hlcarry")
    ap.add_argument("--config", help="override config file (default config/local.toml)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan", help="rank funding carry opportunities, net of costs")
    s.add_argument("--coins", help="comma separated perp names")
    s.add_argument("--json", action="store_true")
    sub.add_parser("record", help="record raw market data until Ctrl-C")
    sub.add_parser("watch", help="re-scan on a timer and alert when a trade clears costs")
    b = sub.add_parser("backtest", help="replay funding history with simple entry/exit rules")
    b.add_argument("--days", type=float, default=180)
    b.add_argument("--coins", default="BTC,ETH,SOL,HYPE")
    b.add_argument("--maker", action="store_true", help="use maker fees instead of taker fees + slippage")
    k = sub.add_parser("kill", help="trip the kill switch")
    k.add_argument("reason", nargs="?", default="manual")
    sub.add_parser("status")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    cfg = load_config(args.config)

    if args.cmd == "scan":
        from .scanner import format_table, scan
        coins = args.coins.split(",") if args.coins else None
        opps = scan(cfg, coins=coins)
        if args.json:
            print(json.dumps([o.as_dict() for o in opps], indent=2))
        else:
            print(format_table(opps, cfg["scanner"]["min_net_edge_bps"], cfg["scanner"]["hold_hours"]))
    elif args.cmd == "record":
        from .recorder import Recorder
        rec = Recorder(cfg)
        loop = asyncio.new_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, rec.stop)
        loop.run_until_complete(rec.run())
    elif args.cmd == "watch":
        from .watch import Watcher
        try:
            Watcher(cfg).run_forever()
        except KeyboardInterrupt:
            pass
    elif args.cmd == "backtest":
        from .backtest import format_results, run
        res = run(cfg, args.coins.split(","), args.days, maker=args.maker)
        print(format_results(res, args.days, args.maker))
    elif args.cmd == "kill":
        from .risk import KillSwitch
        KillSwitch(cfg["risk"]["kill_switch_file"]).trip(args.reason)
        print(f"kill switch tripped; delete {cfg['risk']['kill_switch_file']} to re-arm")
    elif args.cmd == "status":
        kf = Path(cfg["risk"]["kill_switch_file"])
        print("kill switch:", f"TRIPPED ({kf.read_text().strip()})" if kf.exists() else "armed (not tripped)")
        root = Path(cfg["recorder"]["data_dir"])
        if root.exists():
            for d in sorted(p for p in root.iterdir() if p.is_dir()):
                files = list(d.glob("*/*.jsonl.gz"))
                size = sum(f.stat().st_size for f in files)
                print(f"  {d.name:<24} {len(files):>4} files {size/1e6:>8.2f} MB")
        else:
            print("no recorded data yet")


if __name__ == "__main__":
    main()
