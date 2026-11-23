"""RUN ON YOUR MACHINE (needs internet): live A/B check of the new Quality Value Rebound against the ORIGINAL worker.

    python scripts/verify_fundamental_live.py                 # default sample
    python scripts/verify_fundamental_live.py TCS INFY ITC

For each ticker it runs (A) the original `scan_ticker` from original/fundamental/dashboard_worker.py (real yfinance calls) and
(B) the new Data Manager + `evaluate` path, one right after the other, and lists every column that differs.
Expected: identical rows (the 'Updated' column is ignored). If Yahoo changes a value between the two calls you may see a tiny
difference on a price-driven column - re-run once; a PERSISTENT difference is a bug, send me the output.
Needs: pip install gspread google-auth   (only because the original file imports them; they are not used here)
"""
import importlib.util
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mizan.data import DataManager  # noqa: E402
from mizan.data.providers.yahoo import YahooProvider  # noqa: E402
from mizan.screeners.fundamental import FundamentalParams, scan_universe  # noqa: E402

spec = importlib.util.spec_from_file_location("orig_fund", ROOT / "original" / "fundamental" / "dashboard_worker.py")
orig = importlib.util.module_from_spec(spec)
spec.loader.exec_module(orig)

tickers = sys.argv[1:] or ["TCS", "INFY", "RELIANCE", "HDFCBANK", "ITC"]
dm = DataManager(YahooProvider(), tempfile.mkdtemp(), {"throttle": {"requests_per_minute": 20}})
res = scan_universe(tickers, dm, FundamentalParams(), now=datetime.now(timezone.utc))
new = {r["Ticker"]: r for r in res.rows}

bad = 0
for t in tickers:
    a = orig.scan_ticker(f"{t}.NS")
    b = new.get(t)
    if a is None or b is None:
        print(f"{t:<10} original={'skipped' if a is None else 'row'}  new={'missing: ' + res.failed.get(t, res.skipped.get(t, '?')) if b is None else 'row'}")
        bad += (a is None) != (b is None)
        continue
    diff = {k: (a[k], b[k]) for k in a if k != "Updated" and a[k] != b[k]}
    print(f"{t:<10} status={a['Status']:<17} {'IDENTICAL' if not diff else 'DIFFERS: ' + str(diff)}")
    bad += bool(diff)
print("\nRESULT:", "PASS - identical rows" if not bad else f"{bad} ticker(s) differ - see above")
print("failed:", res.failed, "| degraded:", res.degraded)
