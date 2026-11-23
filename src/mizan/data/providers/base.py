"""Provider contract. Screeners never touch a provider; only DataManager does."""
from __future__ import annotations

from datetime import date
from typing import Protocol

import pandas as pd


class Provider(Protocol):
    name: str

    def provider_symbol(self, ticker: str) -> str: ...

    def fetch_ohlcv(self, symbol: str, start: date | None, end: date | None) -> pd.DataFrame:
        """RAW (unadjusted-for-dividends) daily bars incl. AdjClose, Dividends, Splits. `end` is exclusive."""

    def fetch_ohlcv_batch(self, symbols: list[str], start: date | None, end: date | None) -> dict[str, "pd.DataFrame | Exception"]: ...

    def fetch_info(self, symbol: str) -> dict: ...

    def fetch_statements(self, symbol: str) -> dict[str, "pd.DataFrame | None"]: ...
