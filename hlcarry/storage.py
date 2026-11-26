"""Append-only raw storage: gzip JSONL, one file per stream per UTC hour.

Layout: data/raw/<stream>/<YYYY-MM-DD>/<HH>.jsonl.gz
Each line: {"recv_ns": <local receive time>, "stream": ..., "msg": <raw payload>}
Raw first, parse later: we can always rebuild derived tables from these files.
"""
from __future__ import annotations

import gzip
import json
import time
from datetime import datetime, timezone
from pathlib import Path


class HourlyJsonlWriter:
    def __init__(self, root: str | Path, flush_every: int = 200):
        self.root = Path(root)
        self.flush_every = flush_every
        self._files: dict[str, tuple[str, gzip.GzipFile]] = {}
        self._pending = 0

    def _path(self, stream: str, ts_ns: int) -> tuple[str, Path]:
        dt = datetime.fromtimestamp(ts_ns / 1e9, tz=timezone.utc)
        key = dt.strftime("%Y-%m-%d/%H")
        return key, self.root / stream / f"{key}.jsonl.gz"

    def write(self, stream: str, msg, recv_ns: int | None = None) -> None:
        recv_ns = recv_ns or time.time_ns()
        key, path = self._path(stream, recv_ns)
        cur = self._files.get(stream)
        if cur is None or cur[0] != key:
            if cur:
                cur[1].close()
            path.parent.mkdir(parents=True, exist_ok=True)
            # Append mode: a restart within the same hour adds a new gzip member,
            # which gzip readers handle transparently.
            self._files[stream] = (key, gzip.open(path, "at", encoding="utf-8"))
        line = json.dumps({"recv_ns": recv_ns, "stream": stream, "msg": msg}, separators=(",", ":"))
        self._files[stream][1].write(line + "\n")
        self._pending += 1
        if self._pending >= self.flush_every:
            self.flush()

    def flush(self) -> None:
        for _, f in self._files.values():
            f.flush()
        self._pending = 0

    def close(self) -> None:
        for _, f in self._files.values():
            f.close()
        self._files.clear()


def read_stream(root: str | Path, stream: str):
    """Yield records for a stream in file order. For replay and backtests."""
    for path in sorted(Path(root, stream).glob("*/*.jsonl.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as f:
            for line in f:
                yield json.loads(line)
