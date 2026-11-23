"""Quality Value Rebound - scanner (orchestration only; all business rules live in logic.py).

Original flow:  Google-Sheet tickers -> per ticker: Yahoo info, 3y history, statements -> evaluate -> sort -> sheet + data.json
New flow:       Universe Service tickers -> Data Manager (shared cache) -> the SAME evaluate -> same sort -> data.json (+ run report)

Behaviour kept from the original: one bad ticker never stops the run; fewer than 120 bars -> ticker skipped; statements that
cannot be read are treated as empty (the ticker is still evaluated); if nothing produced data the previous output is left untouched.
New (never silent): every failure, skip and degradation is listed in run_report.json; a ticker whose data is unavailable today
keeps its previous row for up to `carry_forward_days` (its 'Updated' column keeps the old time, so it can never pass as fresh).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from ...core.errors import DataUnavailableError
from ...core.io import atomic_write_text
from ...universe.normalize import to_yahoo
from .logic import evaluate
from .params import OUTPUT_COLUMNS, STATUS_ORDER, FundamentalParams

MODULE_ID, DISPLAY_NAME = "fundamental", "Quality Value Rebound"


@dataclass
class ScanResult:
    rows: list = field(default_factory=list)          # sorted, cleaned records (original column order)
    frame: pd.DataFrame | None = None
    failed: dict = field(default_factory=dict)        # ticker -> reason (data unavailable / engine error)
    skipped: dict = field(default_factory=dict)       # ticker -> reason (not enough history)
    degraded: dict = field(default_factory=dict)      # ticker -> what was missing but the ticker was still evaluated
    deferred: list = field(default_factory=list)      # not attempted: provider paused (rate limit)
    carried: list = field(default_factory=list)       # previous row kept because today's data was unavailable
    meta: dict = field(default_factory=dict)


def _parse_updated(s: str) -> datetime | None:
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _sorted_frame(rows: list) -> pd.DataFrame:
    """Same sort and clean-up as the original (status order, then Tech Score descending)."""
    df = pd.DataFrame(rows)
    df["_o"] = df["Status"].map(STATUS_ORDER).fillna(9)
    df = df.sort_values(["_o", "Tech Score"], ascending=[True, False]).drop(columns="_o")
    return df.replace([np.inf, -np.inf], 0).fillna(0)


def scan_universe(tickers, dm, params: FundamentalParams = FundamentalParams(), now: datetime | None = None,
                  previous_rows: list | None = None) -> ScanResult:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    tickers = list(tickers)
    res, rows = ScanResult(), []
    dm.prefetch_ohlcv(tickers, years=params.high_lookback_years)      # batch-warm the cache; failures surface per ticker below

    for sym in tickers:
        try:
            info = dm.get_info(sym).data                                # original: failure here = ticker failed
            hist_r = dm.get_ohlcv(sym, years=params.high_lookback_years)  # adjusted view = yfinance history(auto_adjust=True)
            hist = hist_r.data
            if hist is None or len(hist) < params.min_bars:
                res.skipped[sym] = f"only {0 if hist is None else len(hist)} daily bars (< {params.min_bars})"
                continue
            if hist_r.status == "stale":
                res.failed[sym] = "stale_price: " + "; ".join(hist_r.issues)
                continue
            hist = hist.dropna(subset=["Close", "High", "Low"])
            try:
                st = dm.get_statements(sym).data
                income, balance = st.get("income_stmt"), st.get("balance_sheet")
            except DataUnavailableError as exc:                         # original: statement errors -> treated as empty
                income = balance = None
                res.degraded[sym] = f"statements unavailable ({exc.reason}); evaluated with empty statements"
            rows.append(evaluate(to_yahoo(sym), info, hist, income, balance, now, params))
        except DataUnavailableError as exc:
            res.failed[sym] = f"data: {exc.reason}"
            if exc.reason == "provider_paused":
                res.deferred.append(sym)
        except Exception as exc:                                        # one bad ticker must never break the run
            res.failed[sym] = f"engine: {type(exc).__name__}: {exc}"

    # keep yesterday's row (clearly dated) for tickers we could not refresh today
    prev = {r.get("Ticker"): r for r in (previous_rows or [])}
    for sym in list(res.failed):
        old = prev.get(sym)
        stamp = _parse_updated(old.get("Updated")) if old else None
        if old and stamp and now - stamp <= timedelta(days=params.carry_forward_days):
            rows.append(old)
            res.carried.append(sym)

    if rows:
        res.frame = _sorted_frame(rows)
        res.rows = json.loads(res.frame.to_json(orient="records", force_ascii=False))
    counts = res.frame["Status"].value_counts().to_dict() if res.frame is not None else {}
    res.meta = {
        "module": MODULE_ID, "display_name": DISPLAY_NAME, "run_at": now.isoformat(timespec="seconds"),
        "requested": len(tickers), "evaluated": len(rows) - len(res.carried), "carried_forward": len(res.carried),
        "skipped": len(res.skipped), "failed": len(res.failed), "deferred": len(res.deferred), "degraded": len(res.degraded),
        "by_status": counts,
        "status": "no_data" if not rows else ("partial" if res.failed else "ok"),
    }
    return res


def write_outputs(res: ScanResult, out_dir: str | Path, extra_meta: dict | None = None) -> dict:
    """data.json = same format as the original file (list of records, 4-space indent). Not written when there is no data."""
    out = Path(out_dir)
    report = {**res.meta, **(extra_meta or {}), "failed_detail": res.failed, "skipped_detail": res.skipped,
              "degraded_detail": res.degraded, "deferred_detail": res.deferred, "carried_forward_detail": res.carried}
    paths = {"report": str(atomic_write_text(out / "run_report.json", json.dumps(report, ensure_ascii=False, indent=1) + "\n"))}
    if res.frame is not None:
        text = res.frame.to_json(orient="records", indent=4, force_ascii=False)
        paths["data"] = str(atomic_write_text(out / "data.json", text))
    return paths


def run(universe, dm, out_dir: str | Path, params: FundamentalParams = FundamentalParams(), now: datetime | None = None) -> dict:
    """Full run for one universe snapshot. Returns the run report (also written next to data.json)."""
    out = Path(out_dir)
    prev = None
    if (out / "data.json").exists():
        try:
            prev = json.loads((out / "data.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            prev = None
    res = scan_universe(universe.tickers, dm, params, now, prev)
    stats = dm.stats()
    extra = {"universe_version": universe.version, "universe_count": universe.count, "data_run_id": dm.run_id,
             "downloads": stats["downloads"], "cache_hits": stats["cache_hits"], "breaker_tripped": stats["breaker_tripped"]}
    write_outputs(res, out, extra)
    dm.close()
    return {**res.meta, **extra}
