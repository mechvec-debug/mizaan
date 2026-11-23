"""Swing Setup Radar scanner: universe -> DataManager -> engine -> records. Output keeps the 10 original columns first
(same order, same value formats) and adds one `details` object. One ticker's failure never stops the others."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ...core.errors import DataUnavailableError
from . import indicators as ix
from .config import TechnicalConfig
from .engine import run_engine
from .signals import S_IN_ZONE, S_TRIGGERED, S_WAIT, grade as make_grade, levels, mtf_summary, rr_at_close, sort_key, state_text

import pandas as pd

ORIGINAL_COLUMNS = ["ticker", "action", "entry", "sl", "tp1", "tp2", "tp3", "aligned", "plan", "time_received"]


@dataclass
class ScanResult:
    records: list = field(default_factory=list)
    skipped: dict = field(default_factory=dict)       # ticker -> reason (not enough history, ...)
    failures: dict = field(default_factory=dict)      # ticker -> reason (data unavailable, engine error)
    meta: dict = field(default_factory=dict)


def _num(x):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(float(x), 2)


def build_record(ticker: str, df: pd.DataFrame, cfg: TechnicalConfig, now: datetime, data_status: str,
                 data_issues: list, prev: dict | None) -> dict:
    r = run_engine(df, cfg)
    state = state_text(r)
    mtf = mtf_summary([r.weekly, r.monthly], cfg)
    lv = levels(r, cfg)
    rr = rr_at_close(r)
    last = df.index[-1].date()
    stale = data_status == "stale" or (now.date() - last).days > cfg.stale_bar_days
    g, reason = make_grade(state, mtf.plan, rr, stale, cfg)
    age = (r.bars - 1 - r.impulse_end_bar) if r.zone_side == 1 else None
    retrace = None
    if r.zone_side == 1 and r.impulse_end > r.impulse_start:
        retrace = round((r.impulse_end - r.close) / (r.impulse_end - r.impulse_start) * 100, 1)
    notify = None                                   # price at which to be alerted (additive field, original columns untouched)
    if r.zone_side == 1:
        if state in (S_TRIGGERED, S_IN_ZONE):
            notify = round(r.close, 2)              # already at the entry: act now
        elif state == S_WAIT and r.close > r.z_hi:
            notify = _num(r.z_hi)                   # waiting above the zone: alert when price falls to its top
    today = now.strftime("%Y-%m-%d")
    prev_d = (prev or {}).get("details", {})
    changed = prev is None or prev.get("action") != state
    state_since = today if changed else prev_d.get("state_since", today)
    rec = {"ticker": ticker, "action": state, **lv, "aligned": mtf.aligned, "plan": mtf.plan,
           "time_received": now.strftime("%Y-%m-%d %H:%M:%S")}
    rec["details"] = {
        "grade": g, "reason": reason, "rr_at_close": rr, "retrace_pct": retrace, "setup_age_bars": age,
        "changed_today": bool(changed), "prev_action": (prev or {}).get("action"), "state_since": state_since,
        "notify_price": notify,
        "zone_hi": _num(r.z_hi) if r.zone_side else None, "zone_lo": _num(r.z_lo) if r.zone_side else None,
        "chart_stop": _num(r.chart_stop) if r.zone_side else None, "chart_tp1": _num(r.chart_tp1) if r.zone_side else None,
        "rr_mid": _num(r.rr_mid) if r.zone_side else None,
        "weekly_bias": r.weekly.bias, "monthly_bias": r.monthly.bias,
        "weekly_strength": round(r.weekly.strength, 1), "monthly_strength": round(r.monthly.strength, 1),
        "conviction": round(mtf.conviction, 1), "ema_cross_recent": r.crossed_recent,
        "last_bar": str(last), "bars": r.bars, "data_status": "stale" if stale else data_status, "data_issues": data_issues,
        "extras": ix.extras_latest(df, cfg.extras) if cfg.extras else {},
    }
    return rec


def scan_universe(tickers, dm, cfg: TechnicalConfig, now: datetime | None = None, previous: list | None = None) -> ScanResult:
    now = (now or datetime.now(ZoneInfo(cfg.timezone))).astimezone(ZoneInfo(cfg.timezone))
    prev = {p["ticker"]: p for p in (previous or [])}
    res = ScanResult()
    for t in tickers:
        try:
            dr = dm.get_ohlcv(t, min_bars=cfg.min_bars, adjusted=False)     # D1: RAW prices (tradable entry, = TradingView)
            if dr.status == "partial":
                res.skipped[t] = f"only {dr.rows} daily bars (< {cfg.min_bars})"
                continue
            res.records.append(build_record(t, dr.data, cfg, now, dr.status, dr.issues, prev.get(t)))
        except DataUnavailableError as exc:
            res.failures[t] = f"data: {exc.reason}"
        except Exception as exc:                    # isolate: log and continue with the next ticker
            res.failures[t] = f"engine: {type(exc).__name__}: {exc}"
    res.records.sort(key=sort_key)
    counts: dict = {}
    for r in res.records:
        counts[r["action"]] = counts.get(r["action"], 0) + 1
    grades = {g or "none": sum(1 for r in res.records if r["details"]["grade"] == g) for g in ("A", "B", "C", "")}
    res.meta = {"module": "technical", "run_at": now.isoformat(timespec="seconds"), "requested": len(list(tickers)), "scanned": len(res.records),
                "skipped": len(res.skipped), "failed": len(res.failures), "by_state": counts, "by_grade": grades,
                "cross_lookback_bars": cfg.cross_lookback_bars, "min_bars": cfg.min_bars}
    return res


def write_outputs(res: ScanResult, out_dir: str | Path, extra_meta: dict | None = None) -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    alerts = out / "trading_alerts.json"            # same name/shape family as the original file (list of records)
    alerts.write_text(json.dumps(res.records, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    report = out / "run_report.json"
    report.write_text(json.dumps({**res.meta, **(extra_meta or {}), "skipped_detail": res.skipped, "failed_detail": res.failures},
                                 ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return {"alerts": str(alerts), "report": str(report)}
