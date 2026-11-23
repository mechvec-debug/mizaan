"""Pure OHLCV helpers: standardise provider output, merge, detect adjustment drift, build the adjusted view.

RAW layout (what is stored, never edited by screeners):  Open High Low Close AdjClose Volume Dividends Splits
  Close is split-adjusted by the provider; AdjClose is split+dividend adjusted.
ADJUSTED view (what screeners receive by default): O/H/L/C multiplied by AdjClose/Close, Volume untouched -
  the same construction yfinance uses for history(auto_adjust=True), which the original screeners relied on.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

RAW_COLS = ["Open", "High", "Low", "Close", "AdjClose", "Volume", "Dividends", "Splits"]
_PRICE = ["Open", "High", "Low", "Close", "AdjClose"]


def empty_raw() -> pd.DataFrame:
    d = pd.DataFrame(columns=RAW_COLS)
    d.index = pd.DatetimeIndex([], name="Date")
    return d


def clean_raw(df: pd.DataFrame | None) -> pd.DataFrame:
    if df is None or len(df) == 0:
        return empty_raw()
    d = df.copy().rename(columns={"Adj Close": "AdjClose", "Stock Splits": "Splits"})
    if "Close" not in d:
        raise ValueError("provider frame has no Close column")
    for c in RAW_COLS:
        if c not in d:
            d[c] = 0.0 if c in ("Dividends", "Splits") else (d["Close"] if c == "AdjClose" else np.nan)
    d = d[RAW_COLS]
    idx = pd.to_datetime(d.index)
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    d.index = pd.DatetimeIndex(idx.normalize(), name="Date")
    d = d[~d.index.duplicated(keep="last")].sort_index()
    d = d[d["Close"].notna() & (d["Close"] > 0)]
    d[_PRICE] = d[_PRICE].astype(float).round(6)
    d["AdjClose"] = d["AdjClose"].fillna(d["Close"])
    d["Volume"] = d["Volume"].fillna(0).astype("int64")
    d[["Dividends", "Splits"]] = d[["Dividends", "Splits"]].fillna(0.0).astype(float)
    return d


def merge(existing: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    """New rows win on overlapping dates (the provider is the authority for recent bars)."""
    if existing is None or len(existing) == 0:
        return new.copy()
    if len(new) == 0:
        return existing.copy()
    out = pd.concat([existing, new])
    out = out[~out.index.duplicated(keep="last")].sort_index()
    return out


def drift(existing: pd.DataFrame, new: pd.DataFrame) -> float:
    """Largest relative disagreement on the overlapping dates, of Close and of the AdjClose/Close ratio.
    A dividend, split or bonus revises history at the provider; stored rows then disagree with a fresh fetch."""
    common = existing.index.intersection(new.index)
    if len(common) == 0:
        return 0.0
    a, b = existing.loc[common], new.loc[common]
    d_close = ((a["Close"] - b["Close"]).abs() / b["Close"]).max()
    ra, rb = a["AdjClose"] / a["Close"], b["AdjClose"] / b["Close"]
    d_ratio = ((ra - rb).abs() / rb).max()
    return float(np.nanmax([d_close, d_ratio]))


def to_adjusted(raw: pd.DataFrame) -> pd.DataFrame:
    f = (raw["AdjClose"] / raw["Close"]).where(raw["Close"] > 0)
    f = f.fillna(1.0)
    out = pd.DataFrame({"Open": raw["Open"] * f, "High": raw["High"] * f, "Low": raw["Low"] * f,
                        "Close": raw["Close"] * f, "Volume": raw["Volume"]}, index=raw.index)
    out.index.name = "Date"
    return out


def validate(raw: pd.DataFrame) -> list[str]:
    """Quality notes attached to results (never silently ignored)."""
    issues = []
    if len(raw) == 0:
        return ["no rows"]
    bad_hl = int((raw["High"] < raw["Low"]).sum())
    if bad_hl:
        issues.append(f"{bad_hl} bar(s) with High < Low")
    if (raw["Volume"] == 0).mean() > 0.5:
        issues.append("more than half of the bars have zero volume")
    return issues
