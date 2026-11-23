"""RawStore - the ONLY writer of shared raw data. Git-friendly on purpose:
  * OHLCV  : one plain-text CSV per ticker (append-only in practice -> tiny git diffs), LF line endings, no gz
  * info / statements : one JSON per ticker, NaN -> null, stable key order
  * every write is atomic (temp file + os.replace); every dataset has a sidecar <name>.meta.json
Prices are stored rounded to 6 decimals (removes provider float noise; far below INR paise precision).
"""
from __future__ import annotations

import json
import math
import os
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

from .keys import CacheKey
from .ohlcv import RAW_COLS

_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def safe_name(symbol: str) -> str:
    """Windows-safe, reversible-enough file stem ('^NSEI' -> '_NSEI', 'CON' -> 'CON_')."""
    s = symbol.replace("^", "_").replace("/", "_").replace("\\", "_").replace(":", "_")
    return s + "_" if s.upper() in _RESERVED else s


def jsonable(o):
    if isinstance(o, dict):
        return {str(k): jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [jsonable(v) for v in o]
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (float, np.floating)):
        return None if (math.isnan(o) or math.isinf(o)) else float(o)
    if isinstance(o, (pd.Timestamp,)):
        return o.isoformat()
    if isinstance(o, np.bool_):
        return bool(o)
    return o


def df_to_obj(df: pd.DataFrame | None):
    if df is None or getattr(df, "empty", True):
        return None
    return {"index": [str(i) for i in df.index], "columns": [pd.Timestamp(c).isoformat() if isinstance(c, pd.Timestamp) else str(c) for c in df.columns],
            "data": jsonable(df.values.tolist()), "columns_are_dates": bool(all(isinstance(c, pd.Timestamp) for c in df.columns))}


def obj_to_df(obj) -> pd.DataFrame | None:
    if not obj:
        return None
    cols = pd.to_datetime(obj["columns"]) if obj.get("columns_are_dates") else obj["columns"]
    return pd.DataFrame(obj["data"], index=obj["index"], columns=cols, dtype=float)


class RawStore:
    def __init__(self, root: str | os.PathLike):
        self.root = Path(root)

    # ---------------------------------------------------------------- paths
    def _dir(self, key: CacheKey) -> Path:
        d = self.root / key.data_type / key.source
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _stem(self, key: CacheKey) -> Path:
        return self._dir(key) / safe_name(key.ticker)

    @staticmethod
    def _atomic(path: Path, text: str) -> None:
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".w.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    # ---------------------------------------------------------------- meta
    def read_meta(self, key: CacheKey) -> dict | None:
        p = self._stem(key).with_suffix(".meta.json")
        if not p.exists():
            return None
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)

    def write_meta(self, key: CacheKey, meta: dict) -> None:
        self._atomic(self._stem(key).with_suffix(".meta.json"), json.dumps(jsonable(meta), indent=1, sort_keys=True) + "\n")

    # ---------------------------------------------------------------- ohlcv
    def read_ohlcv(self, key: CacheKey) -> tuple[pd.DataFrame | None, dict | None]:
        p = self._stem(key).with_suffix(".csv")
        meta = self.read_meta(key)
        if not p.exists() or meta is None or meta.get("version") != key.version:
            return None, None                      # missing or written by another schema version -> treated as no cache
        df = pd.read_csv(p, index_col="Date", parse_dates=["Date"])
        df[["Dividends", "Splits"]] = df[["Dividends", "Splits"]].fillna(0.0)
        df["Volume"] = df["Volume"].astype("int64")
        return df[RAW_COLS], meta

    def write_ohlcv(self, key: CacheKey, df: pd.DataFrame, meta: dict) -> None:
        out = df[RAW_COLS].copy()
        for c in ("Dividends", "Splits"):
            out[c] = out[c].where(out[c] != 0, np.nan)          # blank instead of 0.0 -> smaller files
        text = out.to_csv(date_format="%Y-%m-%d", lineterminator="\n")
        self._atomic(self._stem(key).with_suffix(".csv"), text)
        meta = {**meta, "version": key.version, "rows": int(len(df)),
                "first_date": df.index[0].strftime("%Y-%m-%d"), "last_date": df.index[-1].strftime("%Y-%m-%d")}
        self.write_meta(key, meta)

    # ---------------------------------------------------------------- json blobs (info / statements)
    def read_blob(self, key: CacheKey) -> dict | None:
        p = self._stem(key).with_suffix(".json")
        if not p.exists():
            return None
        with open(p, encoding="utf-8") as fh:
            blob = json.load(fh)
        return blob if blob.get("version") == key.version else None

    def write_blob(self, key: CacheKey, fetched_at: str, data: dict) -> None:
        blob = {"version": key.version, "key": str(key), "fetched_at": fetched_at, "data": jsonable(data)}
        self._atomic(self._stem(key).with_suffix(".json"), json.dumps(blob, indent=1, sort_keys=True) + "\n")

    # ---------------------------------------------------------------- reporting
    def size_report(self) -> dict:
        total, files = 0, 0
        by_type: dict[str, int] = {}
        for p in self.root.rglob("*"):
            if p.is_file():
                s = p.stat().st_size
                total, files = total + s, files + 1
                t = p.relative_to(self.root).parts[0]
                by_type[t] = by_type.get(t, 0) + s
        return {"files": files, "bytes": total, "mb": round(total / 1e6, 2), "by_type_mb": {k: round(v / 1e6, 2) for k, v in by_type.items()}}
