"""Download ledger (requirement B): every provider call is a row, so duplicate downloads are MEASURABLE.

ledger/downloads_YYYY-MM.csv : ts,run_id,ticker,data_type,source,period,action,rows,status,duration_ms,attempts,error
ledger/runs/<run_id>.json    : per-run counters (downloads, cache hits, failures) written by flush_run_stats()
"""
from __future__ import annotations

import csv
import json
import threading
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import pandas as pd

COLUMNS = ["ts", "run_id", "ticker", "data_type", "source", "period", "action", "rows", "status", "duration_ms", "attempts", "error"]


class DownloadLedger:
    def __init__(self, directory: str | Path, run_id: str, clock: Callable[[], datetime] | None = None):
        self.dir, self.run_id = Path(directory), run_id
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self.hits: Counter = Counter()
        self.downloads: Counter = Counter()
        self.failures: Counter = Counter()
        self.dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ write
    def record(self, *, ticker, data_type, source, period, action, rows=0, status="ok", duration_ms=0, attempts=1, error="") -> None:
        now = self.clock()
        row = [now.isoformat(timespec="seconds"), self.run_id, ticker, data_type, source, period, action, rows, status,
               int(duration_ms), attempts, str(error).replace("\n", " ")[:300]]
        path = self.dir / f"downloads_{now.strftime('%Y-%m')}.csv"
        with self._lock:
            new = not path.exists()
            with open(path, "a", encoding="utf-8", newline="") as fh:
                w = csv.writer(fh, lineterminator="\n")
                if new:
                    w.writerow(COLUMNS)
                w.writerow(row)
            (self.downloads if status == "ok" else self.failures)[data_type] += 1

    def cache_hit(self, data_type: str) -> None:
        with self._lock:
            self.hits[data_type] += 1

    def flush_run_stats(self) -> Path:
        out = self.dir / "runs"
        out.mkdir(exist_ok=True)
        p = out / f"{self.run_id}.json"
        p.write_text(json.dumps({"run_id": self.run_id, "downloads": dict(self.downloads), "cache_hits": dict(self.hits),
                                 "failures": dict(self.failures)}, indent=1, sort_keys=True), encoding="utf-8")
        return p

    # ------------------------------------------------------------ read / audit
    def read(self, month: str | None = None) -> pd.DataFrame:
        files = sorted(self.dir.glob(f"downloads_{month or '*'}.csv"))
        if not files:
            return pd.DataFrame(columns=COLUMNS)
        return pd.concat([pd.read_csv(f) for f in files], ignore_index=True)

    def counts_for_day(self, day: str) -> pd.DataFrame:
        """The report from the requirements: Ticker | Data type | Date | Source | Download count."""
        df = self.read()
        if df.empty:
            return pd.DataFrame(columns=["ticker", "data_type", "date", "source", "download_count"])
        df = df[(df["status"] == "ok") & (df["ts"].str[:10] == day)]
        g = df.groupby(["ticker", "data_type", df["ts"].str[:10].rename("date"), "source"]).size().reset_index(name="download_count")
        return g

    def duplicates_for_day(self, day: str) -> pd.DataFrame:
        """Same ticker/type/action downloaded more than once on a day = suspected duplicate download."""
        df = self.read()
        if df.empty:
            return df
        df = df[(df["status"] == "ok") & (df["ts"].str[:10] == day)]
        g = df.groupby(["ticker", "data_type", "source", "action"]).size().reset_index(name="count")
        return g[g["count"] > 1]
