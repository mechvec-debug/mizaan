"""Test doubles for the data layer: a scriptable provider and a controllable clock (no network, no real waiting)."""
from __future__ import annotations

import threading
import time
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd


class FakeClock:
    def __init__(self, start="2026-11-03T10:00:00+00:00"):
        self.t = datetime.fromisoformat(start)

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


_CACHE: dict = {}


def make_history(start="2012-01-02", end="2026-11-02", seed=0, base=100.0, adj_ratio=0.98):
    k = (start, end, seed, base, adj_ratio)
    if k not in _CACHE:
        _CACHE[k] = _build(*k)
    return _CACHE[k].copy()


def _build(start, end, seed, base, adj_ratio):
    idx = pd.bdate_range(start, end)
    rng = np.random.default_rng(seed)
    close = np.round(base * np.exp(np.cumsum(rng.normal(0, 0.01, len(idx)))), 2)
    df = pd.DataFrame({"Open": close * 0.998, "High": close * 1.01, "Low": close * 0.99, "Close": close,
                       "Adj Close": close * adj_ratio, "Volume": rng.integers(100_000, 900_000, len(idx)),
                       "Dividends": 0.0, "Stock Splits": 0.0}, index=idx)
    df.index = df.index.tz_localize("Asia/Kolkata")           # yfinance returns tz-aware dates
    return df


class FakeProvider:
    name = "fake"

    def __init__(self, delay=0.0):
        self.data: dict[str, pd.DataFrame] = {}
        self.calls: list[dict] = []
        self.errors: dict[str, list] = {}
        self.info_calls = self.stmt_calls = 0
        self.delay = delay
        self._lock = threading.Lock()

    def add(self, ticker, **kw):
        self.data[ticker + ".FK"] = make_history(**kw)
        return self

    def provider_symbol(self, ticker):
        return ticker if ticker.startswith("^") else ticker + ".FK"

    def _maybe_fail(self, symbol):
        q = self.errors.get(symbol)
        if q:
            exc = q[0]
            if len(q) > 1 or not getattr(exc, "sticky", False):
                q.pop(0)
            raise exc

    def _slice(self, symbol, start, end):
        df = self.data[symbol]
        if start is not None:
            df = df[df.index >= pd.Timestamp(start).tz_localize(df.index.tz)]
        if end is not None:
            df = df[df.index < pd.Timestamp(end).tz_localize(df.index.tz)]
        return df.copy()

    def fetch_ohlcv(self, symbol, start, end):
        with self._lock:
            self.calls.append({"kind": "single", "symbol": symbol, "start": start, "end": end})
        if self.delay:
            time.sleep(self.delay)
        self._maybe_fail(symbol)
        if symbol not in self.data:
            return pd.DataFrame()
        return self._slice(symbol, start, end)

    def fetch_ohlcv_batch(self, symbols, start, end):
        with self._lock:
            self.calls.append({"kind": "batch", "symbols": list(symbols), "start": start, "end": end})
        out = {}
        for s in symbols:
            try:
                self._maybe_fail(s)
                out[s] = self._slice(s, start, end) if s in self.data else pd.DataFrame()
            except Exception as exc:                  # per-symbol failure is returned, not raised
                out[s] = exc
        return out

    def fetch_info(self, symbol):
        with self._lock:
            self.info_calls += 1
        self._maybe_fail(symbol)
        return {"marketCap": 5e10, "trailingPE": float("nan"), "sector": "Industrials", "symbol": symbol}

    def fetch_statements(self, symbol):
        with self._lock:
            self.stmt_calls += 1
        self._maybe_fail(symbol)
        cols = [pd.Timestamp("2026-03-31"), pd.Timestamp("2025-03-31")]
        bs = pd.DataFrame([[9e9, 8e9], [4e9, np.nan]], index=["Total Assets", "Total Debt"], columns=cols)
        return {"income_stmt": None, "balance_sheet": bs, "financials": None}

    # ---- scripted market events
    def extend_to(self, symbol_ticker, end, seed=99):
        sym = symbol_ticker + ".FK"
        df = self.data[sym]
        last = df.index[-1].tz_localize(None)
        more = make_history(start=(last + pd.Timedelta(days=1)).strftime("%Y-%m-%d"), end=end, seed=seed, base=float(df["Close"].iloc[-1]))
        self.data[sym] = pd.concat([df, more])

    def apply_dividend(self, ticker, ex_date, factor=0.97):
        sym = ticker + ".FK"
        df = self.data[sym]
        ex = pd.Timestamp(ex_date).tz_localize(df.index.tz)
        df.loc[df.index < ex, "Adj Close"] *= factor
        df.loc[ex, "Dividends"] = 5.0

    def apply_split(self, ticker, ex_date, ratio=2.0):
        sym = ticker + ".FK"
        df = self.data[sym]
        ex = pd.Timestamp(ex_date).tz_localize(df.index.tz)
        m = df.index < ex
        for c in ("Open", "High", "Low", "Close", "Adj Close"):
            df.loc[m, c] /= ratio
        df.loc[m, "Volume"] = (df.loc[m, "Volume"] * ratio).astype("int64")
        df.loc[ex, "Stock Splits"] = ratio
