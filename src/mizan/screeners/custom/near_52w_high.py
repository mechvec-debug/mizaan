"""EXAMPLE custom screener (disabled by default): stocks trading within 5 % of their 52-week high.

It shows the whole contract of a custom module: a function run(ctx) that reads data ONLY through the Data Manager, writes its
rows to ctx.results_dir(<id>), and returns a small summary dict. Registering it needs one entry in config/modules.yaml and no change
to any existing engine. Copy this file as the starting point for your own module."""
from __future__ import annotations

import json

from ...core.errors import DataUnavailableError
from ...core.io import atomic_write_text

MODULE_ID = "near_52w_high"
THRESHOLD_PCT = 5.0


def run(ctx) -> dict:
    dm, rows, failed = ctx.data_manager(), [], {}
    for t in ctx.tickers(MODULE_ID):
        try:
            d = dm.get_ohlcv(t, years=1, adjusted=False, min_bars=100).data
        except DataUnavailableError as exc:                      # one stock failing must not stop the rest
            failed[t] = exc.reason
            continue
        if len(d) < 100:
            continue
        high, close = float(d["High"].max()), float(d["Close"].iloc[-1])
        below = (1 - close / high) * 100
        rows.append({"ticker": t, "close": round(close, 2), "high_52w": round(high, 2), "pct_below_high": round(below, 2),
                     "near_high": below <= THRESHOLD_PCT})
    rows.sort(key=lambda r: r["pct_below_high"])
    atomic_write_text(ctx.results_dir(MODULE_ID) / "results.json", json.dumps(rows, indent=1) + "\n")
    return {"stocks": len(rows), "near_high": sum(r["near_high"] for r in rows), "failed": len(failed)}
