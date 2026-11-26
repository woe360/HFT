"""Config loading: config/default.toml, overlaid by config/local.toml if present."""
from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    with open(ROOT / "config" / "default.toml", "rb") as f:
        cfg = tomllib.load(f)
    local = Path(path) if path else ROOT / "config" / "local.toml"
    if local.exists():
        with open(local, "rb") as f:
            cfg = _merge(cfg, tomllib.load(f))
    return cfg
