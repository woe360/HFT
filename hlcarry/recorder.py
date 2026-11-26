"""Market data recorder.

Streams l2Book, trades and activeAssetCtx (funding, mark, oracle, OI) over
WebSocket and also polls REST asset contexts as a cross-check. Everything is
written raw with a local receive timestamp. Reconnects with backoff and logs
every connect/disconnect so gaps in the data are visible later.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time

import websockets

from .api import WS_URLS, InfoClient
from .storage import HourlyJsonlWriter

log = logging.getLogger("recorder")


def subscriptions(coins: list[str], channels: list[str]) -> list[dict]:
    return [{"type": ch, "coin": c} for c in coins for ch in channels]


def stream_name(msg: dict) -> str | None:
    """Map a WS message to a storage stream like 'l2Book.ETH'."""
    ch = msg.get("channel")
    data = msg.get("data")
    if ch in (None, "pong", "subscriptionResponse"):
        return None
    coin = None
    if isinstance(data, dict):
        coin = data.get("coin")
    elif isinstance(data, list) and data and isinstance(data[0], dict):
        coin = data[0].get("coin")
    return f"{ch}.{coin}" if coin else ch


class Recorder:
    def __init__(self, cfg: dict):
        rc = cfg["recorder"]
        self.env = cfg["network"]["env"]
        self.coins = rc["coins"]
        self.channels = rc["channels"]
        self.ping_s = rc["ping_seconds"]
        self.poll_s = rc["rest_poll_seconds"]
        self.stale_s = rc["stale_after_seconds"]
        self.writer = HourlyJsonlWriter(rc["data_dir"])
        self.last_msg = time.monotonic()
        self.msg_count = 0
        self._stop = asyncio.Event()

    def stop(self):
        self._stop.set()

    def event(self, kind: str, **kw):
        """System events (connects, gaps, errors) go to their own stream."""
        self.writer.write("_events", {"event": kind, **kw})
        log.info("%s %s", kind, kw)

    async def _ws_session(self):
        url = WS_URLS[self.env]
        async with websockets.connect(url, ping_interval=None, max_size=2**24) as ws:
            self.event("ws_connected", url=url)
            for sub in subscriptions(self.coins, self.channels):
                await ws.send(json.dumps({"method": "subscribe", "subscription": sub}))

            async def pinger():
                while True:
                    await asyncio.sleep(self.ping_s)
                    await ws.send(json.dumps({"method": "ping"}))

            ping_task = asyncio.create_task(pinger())
            try:
                while not self._stop.is_set():
                    raw = await asyncio.wait_for(ws.recv(), timeout=self.stale_s)
                    recv_ns = time.time_ns()
                    self.last_msg = time.monotonic()
                    msg = json.loads(raw)
                    name = stream_name(msg)
                    if name:
                        self.writer.write(name, msg.get("data"), recv_ns)
                        self.msg_count += 1
            finally:
                ping_task.cancel()

    async def run_ws(self):
        backoff = 1.0
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                await self._ws_session()
            except asyncio.TimeoutError:
                self.event("ws_stale", seconds=self.stale_s)
            except Exception as e:  # noqa: BLE001 - we log and reconnect on anything
                self.event("ws_error", error=repr(e))
            self.writer.flush()
            if time.monotonic() - started > 60:
                backoff = 1.0  # it was a healthy session; reset
            delay = min(backoff, 60) * (0.5 + random.random())
            self.event("ws_reconnect_in", seconds=round(delay, 2))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            backoff *= 2

    async def run_rest_poll(self):
        client = InfoClient(self.env)
        while not self._stop.is_set():
            try:
                perp = await asyncio.to_thread(client.perp_meta_and_ctxs)
                self.writer.write("rest.perpCtxs", perp)
                spot = await asyncio.to_thread(client.spot_meta_and_ctxs)
                self.writer.write("rest.spotCtxs", spot)
            except Exception as e:  # noqa: BLE001
                self.event("rest_error", error=repr(e))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_s)
            except asyncio.TimeoutError:
                pass

    async def run_heartbeat(self):
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass
            age = time.monotonic() - self.last_msg
            log.info("heartbeat msgs=%d last_msg_age=%.1fs", self.msg_count, age)
            self.writer.flush()

    async def run(self):
        self.event("recorder_start", coins=self.coins, channels=self.channels, env=self.env)
        try:
            await asyncio.gather(self.run_ws(), self.run_rest_poll(), self.run_heartbeat())
        finally:
            self.event("recorder_stop")
            self.writer.close()
