"""Thin read-only client for Hyperliquid's /info endpoint.

No keys, no signing: this module can never place an order.
"""
from __future__ import annotations

import time
from typing import Any

import requests

URLS = {
    "mainnet": "https://api.hyperliquid.xyz",
    "testnet": "https://api.hyperliquid-testnet.xyz",
}
WS_URLS = {
    "mainnet": "wss://api.hyperliquid.xyz/ws",
    "testnet": "wss://api.hyperliquid-testnet.xyz/ws",
}


class InfoClient:
    def __init__(self, env: str = "mainnet", timeout: float = 10.0, session=None):
        self.base = URLS[env]
        self.timeout = timeout
        self.s = session or requests.Session()

    def _post(self, body: dict) -> Any:
        last = None
        for attempt in range(4):
            try:
                r = self.s.post(f"{self.base}/info", json=body, timeout=self.timeout)
                if r.status_code == 429:
                    raise requests.HTTPError("rate limited", response=r)
                r.raise_for_status()
                return r.json()
            except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as e:
                last = e
                time.sleep(0.5 * 2**attempt)
        raise RuntimeError(f"info request failed after retries: {body['type']}") from last

    def perp_meta_and_ctxs(self):
        return self._post({"type": "metaAndAssetCtxs"})

    def spot_meta_and_ctxs(self):
        return self._post({"type": "spotMetaAndAssetCtxs"})

    def l2_book(self, coin: str):
        return self._post({"type": "l2Book", "coin": coin})

    def funding_history(self, coin: str, start_ms: int, end_ms: int | None = None):
        body = {"type": "fundingHistory", "coin": coin, "startTime": start_ms}
        if end_ms is not None:
            body["endTime"] = end_ms
        return self._post(body)
