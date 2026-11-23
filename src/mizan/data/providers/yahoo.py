"""Yahoo Finance adapter (yfinance). NOT exercised against the live service in the build sandbox (no network):
run scripts/verify_live_parity.py on your machine once to confirm it against the original screeners' downloads."""
from __future__ import annotations

from datetime import date

import pandas as pd

from ...core.errors import PermanentProviderError, RateLimitError, TransientProviderError
from ...universe.normalize import to_yahoo


def _yf():
    import yfinance as yf          # imported lazily: tests and the dashboard don't need it
    return yf


def map_exception(exc: Exception) -> Exception:
    name, msg = type(exc).__name__.lower(), str(exc).lower()
    if "ratelimit" in name or "too many requests" in msg or "429" in msg or "rate limit" in msg:
        return RateLimitError(str(exc))
    if any(k in name for k in ("missing", "notfound", "delisted")) or "delisted" in msg or "no data found" in msg:
        return PermanentProviderError(str(exc))
    return TransientProviderError(f"{type(exc).__name__}: {exc}")


class YahooProvider:
    name = "yahoo"

    def __init__(self, timeout: float = 30.0):
        self.timeout = timeout

    def provider_symbol(self, ticker: str) -> str:
        return ticker if ticker.startswith("^") or "." in ticker else to_yahoo(ticker)

    def fetch_ohlcv(self, symbol: str, start: date | None, end: date | None) -> pd.DataFrame:
        try:
            t = _yf().Ticker(symbol)
            kw = dict(auto_adjust=False, actions=True, timeout=self.timeout)
            return t.history(start=start, end=end, **kw) if start else t.history(period="max", **kw)
        except Exception as exc:
            raise map_exception(exc) from exc

    def fetch_ohlcv_batch(self, symbols: list[str], start: date | None, end: date | None) -> dict:
        try:
            df = _yf().download(symbols, start=start, end=end, auto_adjust=False, actions=True, group_by="ticker",
                                threads=True, progress=False, timeout=self.timeout)
        except Exception as exc:
            err = map_exception(exc)
            return {s: err for s in symbols}
        out: dict = {}
        multi = isinstance(df.columns, pd.MultiIndex)
        for s in symbols:
            try:
                sub = df[s] if multi else df
                out[s] = sub.dropna(how="all")
            except KeyError:
                out[s] = pd.DataFrame()
        return out

    def fetch_info(self, symbol: str) -> dict:
        try:
            t = _yf().Ticker(symbol)
            info = dict(t.info or {})
            try:
                info["_fast_market_cap"] = getattr(t.fast_info, "market_cap", None)
            except Exception:
                info["_fast_market_cap"] = None
        except Exception as exc:
            raise map_exception(exc) from exc
        if len(info) < 3:
            raise TransientProviderError("empty info response")
        return info

    def fetch_statements(self, symbol: str) -> dict:
        try:
            t = _yf().Ticker(symbol)
            return {"income_stmt": t.income_stmt, "balance_sheet": t.balance_sheet, "financials": t.financials}
        except Exception as exc:
            raise map_exception(exc) from exc
