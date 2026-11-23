"""RUN THIS ON YOUR MACHINE (needs internet): confirms the new data layer reproduces what the ORIGINAL screeners downloaded.

    python scripts/verify_live_parity.py            # default sample
    python scripts/verify_live_parity.py TCS INFY RELIANCE

For each ticker it compares
  A) original call      : yfinance.Ticker(sym + '.NS').history(period='3y')          (Fundamental screener)
  B) new data layer     : DataManager(YahooProvider).get_ohlcv(ticker, years=3)      (adjusted view)
and prints bar-count difference and the largest relative price difference on shared dates.
Expected: identical dates (+-1 boundary bar) and relative differences < 1e-6. Send me the output if anything is larger.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import yfinance as yf  # noqa: E402

from mizan.data import DataManager  # noqa: E402
from mizan.data.providers.yahoo import YahooProvider  # noqa: E402

tickers = sys.argv[1:] or ["TCS", "INFY", "RELIANCE", "M&M", "BAJAJ-AUTO"]
dm = DataManager(YahooProvider(), tempfile.mkdtemp(), {"throttle": {"requests_per_minute": 20}})
worst = 0.0
for t in tickers:
    orig = yf.Ticker(f"{t}.NS").history(period="3y")
    orig.index = pd.to_datetime(orig.index).tz_localize(None).normalize()
    new = dm.get_ohlcv(t, years=3)
    d = new.data
    shared = orig.index.intersection(d.index)
    rel = max(float(((orig.loc[shared, c] - d.loc[shared, c]).abs() / orig.loc[shared, c]).max()) for c in ["Open", "High", "Low", "Close"])
    vol = int((orig.loc[shared, "Volume"] != d.loc[shared, "Volume"]).sum())
    worst = max(worst, rel)
    print(f"{t:<12} bars orig={len(orig):4d} new={len(d):4d} shared={len(shared):4d} "
          f"max_rel_price_diff={rel:.2e} volume_mismatches={vol} status={new.status} issues={new.issues}")
print("\nWORST relative price difference:", f"{worst:.2e}", "(PASS if < 1e-6)" )
print("Download ledger rows written:", len(dm.ledger.read()))
