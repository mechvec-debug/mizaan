"""Build data/universe/nse_bse_map.csv (NSE symbol -> BSE scrip code) by matching ISINs.

  1. download the NSE equity list (EQUITY_L.csv) and the BSE list of scrips (csv) from the exchanges' websites
  2. python scripts/build_bse_map.py EQUITY_L.csv ListOfScrips.csv
The script reports how many of YOUR universe symbols got a code; unmapped ones simply use the Yahoo statements path.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mizan.core.paths import project_root  # noqa: E402
from mizan.screeners.shariah.mapper import build_map, write_map  # noqa: E402
from mizan.universe import UniverseService  # noqa: E402

if len(sys.argv) < 3:
    sys.exit(__doc__)
out = Path(sys.argv[3]) if len(sys.argv) > 3 else project_root() / "data" / "universe" / "nse_bse_map.csv"
rows, rep = build_map(sys.argv[1], sys.argv[2])
write_map(rows, out)
uni = set(UniverseService().get().tickers)
mapped = {r["symbol"] for r in rows}
print(f"NSE rows: {rep['nse_rows']}   matched by ISIN: {rep['mapped']}   written to {out}")
print(f"your universe: {len(uni)} symbols, {len(uni & mapped)} mapped, {len(uni - mapped)} without a BSE code")
print("unmapped (first 20):", sorted(uni - mapped)[:20])
if rep["seed_conflicts"]:
    print("WARNING - differs from the codes hard-coded in the original screener:", rep["seed_conflicts"])
