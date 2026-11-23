"""PARITY SPOT-CHECK against TradingView (run on your machine, needs internet).

    python scripts/tradingview_spotcheck.py TCS INFY RELIANCE

Prints, for each ticker, what the new Python engine says for the LAST DAILY BAR. Open the same stock on TradingView (1D chart,
your indicator, NSE) on the same day and compare:
    state       <-> the HUD chip (IN BUY ZONE / WAIT FOR BUY ZONE / TARGET HIT / STOPPED / Trend - no setup)
    zone        <-> the blue zone's high / low prices
    chart stop / TP1  <-> the red / green lines
    weekly / monthly bias + strength <-> the HUD table rows (arrow + strength bar)
    entry/sl/tp1-3    <-> the BUY label (ATR x 2, 1R / 2R / 3R) - shown on every row, hypothetical unless TRIGGERED
Any mismatch is a BUG to report to me, not a rule to change. Send me: ticker, date, and what TradingView shows.
"""
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mizan.data import DataManager  # noqa: E402
from mizan.data.providers.yahoo import YahooProvider  # noqa: E402
from mizan.screeners.technical import load_technical_config, scan_universe  # noqa: E402

tickers = sys.argv[1:] or ["TCS", "INFY", "RELIANCE", "HDFCBANK", "TATAMOTORS"]
cfg = load_technical_config()
dm = DataManager(YahooProvider(), tempfile.mkdtemp(), {"throttle": {"requests_per_minute": 20}})
res = scan_universe(tickers, dm, cfg, now=datetime.now(ZoneInfo(cfg.timezone)))
for r in res.records:
    d = r["details"]
    print("=" * 78)
    print(f"{r['ticker']}   last bar {d['last_bar']}   bars {d['bars']}   data {d['data_status']} {d['data_issues'] or ''}")
    print(f"  state      : {r['action']}      grade {d['grade'] or '-'}   ({d['reason']})")
    print(f"  zone       : {d['zone_lo']} - {d['zone_hi']}     chart stop {d['chart_stop']}   chart TP1 {d['chart_tp1']}   R:R at close {d['rr_at_close']}")
    print(f"  weekly     : bias {d['weekly_bias']:+d}  strength {d['weekly_strength']}      monthly: bias {d['monthly_bias']:+d}  strength {d['monthly_strength']}")
    print(f"  aligned    : {r['aligned']}      plan: {r['plan']}")
    print(f"  levels     : entry {r['entry']}  SL {r['sl']}  TP1 {r['tp1']}  TP2 {r['tp2']}  TP3 {r['tp3']}")
for t, why in {**res.skipped, **res.failures}.items():
    print(f"{t}: NOT SCANNED - {why}")
