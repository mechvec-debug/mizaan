"""NSE symbol -> BSE scrip code (needed to find the XBRL filing).

The original used 10 hard-coded codes plus an optional JSON that was never shipped, so 1,223 of 1,228 stocks had no code.
Now: data/universe/nse_bse_map.csv (symbol,bse_code,isin,name) built from the two public master lists by ISIN:
  python scripts/build_bse_map.py NSE_EQUITY_L.csv BSE_LIST_OF_SCRIPS.csv   (download both from the exchanges' websites)
Unmapped symbols resolve to None (-> 'N/A'): the screener then uses the Yahoo statements path; nothing is invented.
"""
from __future__ import annotations

import csv
import io
from pathlib import Path

from ...core.io import atomic_write_text
from ...universe.normalize import normalize_symbol

SEED = {"RELIANCE": "500325", "TCS": "532540", "INFY": "500209", "HDFCBANK": "500180", "ICICIBANK": "532174", "ITC": "500875",
        "LT": "500510", "HINDUNILVR": "500696", "BHARTIARTL": "532454", "SBIN": "500112"}      # from the original DEFAULT_MAP


class BseMapper:
    def __init__(self, csv_path: str | Path | None = None):
        self.map = dict(SEED)
        if csv_path and Path(csv_path).exists():
            with open(csv_path, encoding="utf-8-sig", newline="") as fh:
                for r in csv.DictReader(fh):
                    sym, code = normalize_symbol(r.get("symbol", "")), (r.get("bse_code") or "").strip()
                    if sym and code:
                        self.map[sym] = code

    def resolve(self, symbol: str) -> str | None:
        return self.map.get(normalize_symbol(symbol))


def _read(path, want, optional=()):
    """Read a CSV with tolerant header matching. want: {name: [candidate header substrings]}; names in `optional` may be absent."""
    with open(path, encoding="utf-8-sig", newline="") as fh:
        rd = csv.reader(fh)
        header = [h.strip().lower() for h in next(rd)]
        idx = {}
        for name, cands in want.items():
            for c in cands:
                hit = next((i for i, h in enumerate(header) if c in h), None)
                if hit is not None:
                    idx[name] = hit
                    break
            else:
                if name in optional:
                    continue
                raise ValueError(f"{path}: no column matching any of {cands}; found {header}")
        return [{**{k: "" for k in optional}, **{k: (row[i].strip() if i < len(row) else "") for k, i in idx.items()}} for row in rd]


def build_map(nse_csv, bse_csv) -> tuple[list[dict], dict]:
    nse = _read(nse_csv, {"symbol": ["symbol"], "isin": ["isin"], "name": ["name of company", "company"]}, optional=("name",))
    bse = _read(bse_csv, {"code": ["security code", "scrip code", "code"], "isin": ["isin"]})
    by_isin = {r["isin"]: r["code"] for r in bse if r["isin"] and r["code"]}
    rows, unmatched, conflicts = [], [], []
    for r in nse:
        sym, isin = normalize_symbol(r["symbol"]), r["isin"]
        code = by_isin.get(isin)
        if not code:
            unmatched.append(sym)
            continue
        if sym in SEED and SEED[sym] != code:
            conflicts.append((sym, SEED[sym], code))
        rows.append({"symbol": sym, "bse_code": code, "isin": isin, "name": r.get("name", "")})
    return rows, {"nse_rows": len(nse), "mapped": len(rows), "unmatched": unmatched, "seed_conflicts": conflicts}


def write_map(rows: list[dict], path: str | Path) -> Path:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["symbol", "bse_code", "isin", "name"])
    for r in sorted(rows, key=lambda r: r["symbol"]):
        w.writerow([r["symbol"], r["bse_code"], r["isin"], r["name"]])
    return atomic_write_text(path, buf.getvalue())
