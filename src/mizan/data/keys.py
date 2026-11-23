"""Cache keys: Ticker + Data Type + Source + Period + Relevant Data Version  (requirement 5).

* data_type : ohlcv | info | statements | xbrl | benchmark ...
* period    : granularity / statement kind (e.g. "1d"). One growing dataset per key; the *coverage* (first/last date)
              is tracked in its metadata, so requests for a different window reuse the same dataset and only the
              missing part is fetched.
* version   : schema / normalisation version of the stored data. Bumping it invalidates old cache entries on purpose.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

DATA_VERSIONS = {"ohlcv": "v1", "info": "v1", "statements": "v1", "xbrl": "v1"}


@dataclass(frozen=True)
class CacheKey:
    ticker: str
    data_type: str
    source: str
    period: str
    version: str

    @classmethod
    def make(cls, ticker: str, data_type: str, source: str, period: str) -> "CacheKey":
        return cls(ticker, data_type, source, period, DATA_VERSIONS[data_type])

    def __str__(self) -> str:
        return "|".join((self.data_type, self.source, self.ticker, self.period, self.version))

    @property
    def digest(self) -> str:
        return hashlib.sha1(str(self).encode()).hexdigest()[:12]
