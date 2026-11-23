"""Show the three screeners' latest results side by side (reads data_store/results/*). Run after the screeners:

    python -m mizan.screeners.technical ; python -m mizan.screeners.fundamental ; python -m mizan.screeners.shariah
    python scripts/cross_check.py [--all]       # default: only today's technical setups

Writes data_store/results/cross_view.json and prints a table. Scores are never mixed: each column is that screener's own verdict.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mizan.core.config import load_config  # noqa: E402
from mizan.core.paths import resolve  # noqa: E402
from mizan.overview import write_cross_view  # noqa: E402

store = resolve(load_config()["paths"]["store"])
view = write_cross_view(store)
s = view["summary"]
print(f"stocks={s['stocks']}  technical={s['in_technical']}  fundamental={s['in_fundamental']}  shariah={s['in_shariah']}")
print(f"today's technical setups: {s['technical_actionable']}  ->  by Shariah verdict: {s['technical_actionable_by_shariah']}")
print("setups that are Shariah COMPLIANT   :", s["technical_actionable_and_shariah_compliant"] or "none yet")
print("setups still Shariah UNDETERMINED   :", len(s["technical_actionable_shariah_undetermined"]), "(need annual-report evidence)")
print(f"\n{'TICKER':<12}{'TECHNICAL':<24}{'GR':<4}{'NOTIFY':>9}{'  FUNDAMENTAL':<20}{'SHARIAH'}")
rows = view["rows"] if "--all" in sys.argv else [r for r in view["rows"] if r["technical"] and r["technical"]["actionable"]]
for r in rows[:60]:
    t, f, sh = r["technical"], r["fundamental"], r["shariah"]
    print(f"{r['ticker']:<12}{(t['action'] if t else '-'):<24}{(t['grade'] if t else ''):<4}{str((t or {}).get('notify_price') or ''):>9}"
          f"  {(f['status'] if f else '-'):<18}{(sh['verdict'] if sh else '-')}")
print(f"\nFull table: {store / 'results' / 'cross_view.json'}")
