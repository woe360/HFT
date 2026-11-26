import asyncio
import gzip
import json

import pytest
import websockets

from hlcarry.config import load_config
from hlcarry.costs import slippage_bps, walk_book
from hlcarry.paper import PaperExecutor
from hlcarry.recorder import Recorder, stream_name
from hlcarry.risk import Book, KillSwitch, Limits, Order, RiskEngine
from hlcarry.scanner import evaluate, match_markets, scan
from hlcarry.storage import read_stream

FEES = {"perp_taker": 0.00045, "perp_maker": 0.00015, "spot_taker": 0.0007, "spot_maker": 0.0004}


def book(mid, spread=0.0002, depth_usd=50_000, levels=5):
    half = mid * spread / 2
    mk = lambda sgn: [
        {"px": str(mid + sgn * (half + i * mid * 0.0001)), "sz": str(depth_usd / levels / mid), "n": 1}
        for i in range(levels)
    ]
    return {"coin": "X", "time": 0, "levels": [mk(-1), mk(+1)]}


PERP = [
    {"universe": [{"name": "ETH", "szDecimals": 4}, {"name": "FOO", "szDecimals": 1}, {"name": "BAR"}]},
    [
        {"funding": "0.0000125", "markPx": "3000", "dayNtlVlm": "1e9"},
        {"funding": "0.0001", "markPx": "1", "dayNtlVlm": "1e6"},
        {"funding": "0.0001", "markPx": "1"},
    ],
]
SPOT = [
    {
        "tokens": [{"name": "USDC", "index": 0}, {"name": "UETH", "index": 5}, {"name": "FOO", "index": 7}],
        "universe": [
            {"name": "@151", "tokens": [5, 0], "index": 151},
            {"name": "FOO/USDC", "tokens": [7, 0], "index": 3},
        ],
    },
    [],
]


def test_match_markets_uses_aliases_and_skips_unhedgeable():
    ms = match_markets(PERP, SPOT, {"ETH": "UETH"})
    assert [(m.coin, m.spot_coin) for m in ms] == [("ETH", "@151"), ("FOO", "FOO/USDC")]


def test_walk_book_and_slippage():
    b = book(100.0, spread=0.001, depth_usd=10_000)
    f = walk_book(b["levels"][1], 1_000)
    assert f.complete and f.avg_px > 100
    bps, ok = slippage_bps(b, "buy", 1_000)
    assert ok and 4.9 < bps < 6  # ~half spread
    _, ok = slippage_bps(b, "buy", 1e9)
    assert not ok


def test_evaluate_math():
    m = match_markets(PERP, SPOT, {"ETH": "UETH"})[0]
    hist = [{"fundingRate": "0.0000125"}] * 72  # ~11% APR
    o = evaluate(m, hist, book(3001), book(3000), FEES, 1000, 168, 2.0)
    assert o.carry_bps == pytest.approx(0.0000125 * 168 * 1e4)  # 21 bps
    assert o.fee_bps == pytest.approx(23.0)
    assert o.net_edge_bps == pytest.approx(o.carry_bps - o.fee_bps - o.slippage_bps)
    assert o.net_edge_bps < 0  # 11% APR does not pay for a 1-week round trip at base fees
    assert o.basis_bps == pytest.approx(1 / 3000 * 1e4, rel=1e-3)
    o2 = evaluate(m, hist, book(3001), book(3000), FEES, 1000, 24 * 60, 2.0)
    assert o2.net_edge_bps > 0  # but it does over 60 days


class FakeClient:
    def perp_meta_and_ctxs(self): return PERP
    def spot_meta_and_ctxs(self): return SPOT
    def l2_book(self, coin): return book(3000 if coin in ("ETH", "@151") else 1)
    def funding_history(self, coin, start):
        r = "0.00005" if coin == "ETH" else "-0.0001"
        return [{"fundingRate": r}] * 72


def test_scan_end_to_end_filters_negative_funding():
    cfg = load_config()
    opps = scan(cfg, client=FakeClient())
    assert [o.coin for o in opps] == ["ETH"]
    assert opps[0].net_edge_bps > 0


def test_scan_skips_mismatched_spot():
    class Mismatch(FakeClient):
        def l2_book(self, coin):
            return book(3000 if coin == "ETH" else 1)  # spot "UETH" priced like a different token
    assert scan(load_config(), client=Mismatch()) == []


def mk_risk(tmp_path, equity=10_000, **kw):
    lim = dict(max_notional_per_coin_usd=2000, max_gross_notional_usd=5000, max_leverage=2.0,
               max_hedge_imbalance=0.05, max_daily_loss_usd=100, max_orders_per_minute=30,
               kill_switch_file=str(tmp_path / "KILL"))
    lim.update(kw)
    bk = Book(equity_usd=equity, day_start_equity_usd=equity)
    ex = PaperExecutor(bk, FEES)
    kill = KillSwitch(lim["kill_switch_file"], ex)
    eng = RiskEngine(Limits(**lim), bk, kill)
    ex.risk = eng
    return bk, ex, kill, eng


def test_risk_limits(tmp_path):
    bk, ex, kill, eng = mk_risk(tmp_path)
    assert eng.check_order(Order("ETH", "perp", "sell", 0.5, 3000)).ok
    assert "per-coin" in eng.check_order(Order("ETH", "perp", "sell", 1, 3000)).reason
    bk.last_data_ts -= 999
    assert eng.check_order(Order("ETH", "perp", "sell", 0.1, 3000)).reason == "market data stale"


def test_carry_trade_paper_flow_and_hedge_check(tmp_path):
    bk, ex, kill, eng = mk_risk(tmp_path)
    eb, sb = book(3001), book(3000)
    assert ex.submit(Order("@151", "spot", "buy", 1000 / 3000, 3000), sb)["status"] == "filled"
    assert not eng.check_hedges([("ETH", "@151")]).ok  # only one leg on
    assert ex.submit(Order("ETH", "perp", "sell", 1000 / 3001, 3001), eb)["status"] == "filled"
    assert eng.check_hedges([("ETH", "@151")]).ok
    paid = ex.apply_funding("ETH", 0.0001, 3001)
    assert paid > 0  # short perp receives positive funding


def test_daily_loss_trips_kill_switch_and_flattens(tmp_path):
    bk, ex, kill, eng = mk_risk(tmp_path)
    ex.submit(Order("ETH", "perp", "sell", 0.3, 3000), book(3000))
    ex.mark({("perp", "ETH"): 3400})  # short loses ~120
    d = eng.check_order(Order("ETH", "perp", "sell", 0.01, 3400))
    assert not d.ok and kill.tripped
    assert (tmp_path / "KILL").exists()
    assert all(q == 0 for q in bk.positions.values())
    # sticky: still rejecting, even reducing orders, until a human re-arms
    assert not eng.check_order(Order("ETH", "perp", "buy", 0.01, 3400)).ok


def test_kill_file_from_shell_trips(tmp_path):
    bk, ex, kill, eng = mk_risk(tmp_path)
    (tmp_path / "KILL").write_text("ops")
    assert not eng.check_order(Order("ETH", "perp", "sell", 0.1, 3000)).ok


def test_reducing_order_allowed_over_limit(tmp_path):
    bk, ex, kill, eng = mk_risk(tmp_path)
    bk.positions[("perp", "ETH")] = -1.0
    bk.prices[("perp", "ETH")] = 3000  # 3000 notional > per-coin 2000
    assert not eng.check_order(Order("ETH", "perp", "sell", 0.1, 3000)).ok
    assert eng.check_order(Order("ETH", "perp", "buy", 0.5, 3000)).ok


def test_stream_name():
    assert stream_name({"channel": "l2Book", "data": {"coin": "ETH"}}) == "l2Book.ETH"
    assert stream_name({"channel": "trades", "data": [{"coin": "BTC"}]}) == "trades.BTC"
    assert stream_name({"channel": "pong"}) is None


def test_recorder_against_fake_ws(tmp_path, monkeypatch):
    """Fake server: accepts subs, sends messages, drops the connection once. Recorder must reconnect."""
    connections = []

    async def handler(ws):
        connections.append(1)
        subs = [json.loads(await ws.recv()) for _ in range(2)]
        assert all(s["method"] == "subscribe" for s in subs)
        for i in range(3):
            await ws.send(json.dumps({"channel": "l2Book", "data": {"coin": "ETH", "time": i, "levels": [[], []]}}))
        if len(connections) == 1:
            await ws.close()  # simulate drop
            return
        await asyncio.sleep(5)

    async def go():
        async with websockets.serve(handler, "127.0.0.1", 0) as srv:
            port = srv.sockets[0].getsockname()[1]
            monkeypatch.setitem(__import__("hlcarry.recorder").recorder.WS_URLS, "mainnet", f"ws://127.0.0.1:{port}")
            cfg = load_config()
            cfg["recorder"].update(coins=["ETH"], channels=["l2Book", "trades"], data_dir=str(tmp_path),
                                   rest_poll_seconds=999, ping_seconds=999, stale_after_seconds=5)
            rec = Recorder(cfg)
            rec.run_rest_poll = lambda: asyncio.sleep(0)  # no network in tests
            task = asyncio.create_task(rec.run())
            for _ in range(100):
                await asyncio.sleep(0.05)
                if rec.msg_count >= 6:
                    break
            rec.stop()
            await asyncio.wait_for(task, 5)
            return rec

    rec = asyncio.run(go())
    assert len(connections) == 2
    recs = list(read_stream(tmp_path, "l2Book.ETH"))
    assert len(recs) == 6 and all("recv_ns" in r for r in recs)
    events = [r["msg"]["event"] for r in read_stream(tmp_path, "_events")]
    assert events.count("ws_connected") == 2 and "ws_reconnect_in" in events


# --- backtest ---
from hlcarry.backtest import fetch_funding, simulate
from hlcarry.watch import Watcher


def test_simulate_no_lookahead_and_costs():
    hot, cold = 0.0001, 0.0  # 87.6% APR vs 0
    rates = [cold] * 24 + [hot] * 48 + [cold] * 48
    r = simulate("X", rates, enter_apr=0.30, exit_apr=0.15, round_trip_cost_bps=27, signal_hours=24)
    assert r.trades == 1
    # Signal crosses 30% only after ~9 hot hours are in the window, so we earn less than all 48.
    assert 0 < r.funding_bps < 48 * hot * 1e4
    assert r.net_bps == pytest.approx(r.funding_bps - 27)
    flat = simulate("X", [cold] * 100, 0.30, 0.15, 27, 24)
    assert flat.trades == 0 and flat.net_bps == 0


def test_fetch_funding_paginates_and_dedups():
    now = 1_000 * 3600 * 1000
    allh = [{"time": now - h * 3600_000, "fundingRate": "0.00001"} for h in range(1, 1201)]

    class C:
        calls = 0
        def funding_history(self, coin, start, end):
            C.calls += 1
            page = sorted([x for x in allh if start <= x["time"] <= end], key=lambda x: x["time"])
            return page[:500]
    got = fetch_funding(C(), "X", days=50, now_ms=now)
    assert len(got) == 1200 and C.calls >= 3
    assert [t for t, _ in got] == sorted(t for t, _ in got)


# --- watcher ---
def test_watcher_alerts_once_per_cooldown(tmp_path):
    cfg = load_config()
    cfg["recorder"]["data_dir"] = str(tmp_path / "raw")
    t = [0.0]
    sent = []
    good = evaluate(match_markets(PERP, SPOT, {"ETH": "UETH"})[0],
                    [{"fundingRate": "0.0002"}] * 72, book(3001), book(3000), FEES, 1000, 168, 2.0)
    w = Watcher(cfg, scan_fn=lambda c: [good], notify_fn=lambda a, b: sent.append(a), clock=lambda: t[0])
    assert w.tick() and sent == ["hl-carry TAKER: ETH"]
    t[0] += 3600
    assert not w.tick()
    t[0] += 6 * 3600
    assert w.tick() and len(sent) == 2
    assert len((tmp_path / "alerts.jsonl").read_text().splitlines()) == 2


# --- paper carry loop ---
from hlcarry.paper_carry import PaperCarry


class LiveishClient(FakeClient):
    """Fake exchange: HYPE perp + @107 spot, funding prints appear as time moves."""
    def __init__(self):
        self.now_ms = 1_800_000_000_000
        self.rate = "0.00002"
        self.px = 30.0
    def perp_meta_and_ctxs(self):
        return [{"universe": [{"name": "HYPE"}]}, [{"funding": self.rate, "markPx": str(self.px), "openInterest": "1"}]]
    def spot_meta_and_ctxs(self):
        return [{"tokens": [{"name": "USDC", "index": 0}, {"name": "HYPE", "index": 150}],
                 "universe": [{"name": "@107", "tokens": [150, 0], "index": 107}]}, []]
    def l2_book(self, coin):
        return book(self.px * (1.0003 if coin == "HYPE" else 1.0))
    def funding_history(self, coin, start, end=None):
        hours = range(start // 3600_000 + 1, self.now_ms // 3600_000 + 1)
        return [{"time": h * 3600_000, "fundingRate": self.rate} for h in hours][-500:]


def paper_cfg(tmp_path):
    cfg = load_config()
    cfg["paper"]["state_dir"] = str(tmp_path / "paper")
    cfg["risk"]["kill_switch_file"] = str(tmp_path / "KILL")
    return cfg


def test_paper_carry_enters_collects_funding_and_persists(tmp_path):
    c = LiveishClient()
    clock = lambda: c.now_ms / 1000
    pc = PaperCarry(paper_cfg(tmp_path), client=c, clock=clock)
    pc.tick()
    assert pc.state["status"] == "in_carry"
    assert pc.risk.check_hedges([("HYPE", "@107")]).ok
    fees = pc.state["fees_paid"]
    assert 0 < fees < 2
    c.now_ms += 3 * 3600_000  # 3 funding prints
    pc.tick()
    assert len(pc.state["funding"]) == 3
    assert pc.state["funding_earned"] == pytest.approx(3 * 1000 * 0.00002, rel=0.01)
    # restart from disk: same state, no double-counted funding
    pc2 = PaperCarry(paper_cfg(tmp_path), client=c, clock=clock)
    pc2.tick()
    assert pc2.state["status"] == "in_carry" and len(pc2.state["funding"]) == 3
    assert pc2.book.positions[("spot", "@107")] > 0


def test_paper_carry_exits_on_negative_funding_and_respects_kill(tmp_path):
    c = LiveishClient()
    clock = lambda: c.now_ms / 1000
    pc = PaperCarry(paper_cfg(tmp_path), client=c, clock=clock)
    pc.tick()
    c.rate = "-0.00001"
    c.now_ms += 3600_000
    pc.tick()
    assert pc.state["status"] == "flat"
    assert all(q == 0 for q in pc.book.positions.values())
    c.rate = "0.0001"
    (tmp_path / "KILL").write_text("ops")
    pc.tick()
    assert pc.state["status"] == "killed"
    assert not any(pc.book.positions.values())
