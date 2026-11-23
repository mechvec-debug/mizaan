"""Data Acquisition Manager (Phase 4): shared cache, ledger, throttling, incremental OHLCV."""
from .keys import CacheKey
from .ledger import DownloadLedger
from .manager import DataManager, DataResult, PrefetchReport

__all__ = ["DataManager", "DataResult", "PrefetchReport", "CacheKey", "DownloadLedger"]
