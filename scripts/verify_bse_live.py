"""RUN ON YOUR MACHINE (needs internet): live check of the BSE XBRL adapter and parser, cross-checked against Yahoo.

    python scripts/verify_bse_live.py                 # TCS INFY RELIANCE
    python scripts/verify_bse_live.py ITC HDFCBANK

For each stock: finds the latest ANNUAL XBRL filing, parses it, prints fiscal year / unit / key figures, and compares total assets
with Yahoo's balance sheet for the same year (a matching ratio proves the unit and the parser). Send me the output if anything
shows 'not found', a scale of None, or a mismatch - those are exactly the cases to fix before trusting the XBRL path.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mizan.core.paths import project_root  # noqa: E402
from mizan.data import DataManager  # noqa: E402
from mizan.data.providers.bse import BseXbrlProvider  # noqa: E402
from mizan.data.providers.yahoo import YahooProvider  # noqa: E402
from mizan.screeners.shariah.bundles import bundle_from_xbrl, bundle_from_yahoo  # noqa: E402
from mizan.screeners.shariah.mapper import BseMapper  # noqa: E402
from mizan.screeners.shariah.xbrl import parse_xbrl_facts  # noqa: E402

mapper = BseMapper(project_root() / "data" / "universe" / "nse_bse_map.csv")
bse = BseXbrlProvider(parse_xbrl_facts)
dm = DataManager(YahooProvider(), tempfile.mkdtemp(), {"throttle": {"requests_per_minute": 20}})
for t in sys.argv[1:] or ["TCS", "INFY", "RELIANCE"]:
    code = mapper.resolve(t)
    print(f"\n=== {t}  (BSE code {code})")
    if not code:
        print("  no BSE code in data/universe/nse_bse_map.csv - build it with scripts/build_bse_map.py")
        continue
    r = bse.fetch_xbrl_facts(code)
    if not r["found"]:
        print("  NOT FOUND:", {k: r[k] for k in ("candidates", "parsed")})
        continue
    x = r["facts"]
    b = bundle_from_xbrl(x)
    f = x["facts"]
    print(f"  filing {r['filing']['attachment']}  FY end {x['fy_end']}  period {x['period_days']} days  scale {x['scale']}")
    print("  facts found:", ", ".join(sorted(f)))
    print("  total assets (filing units):", f.get("total_assets", {}).get("value"), "| liabilities tag:", f.get("total_liabilities", {}).get("tag"))
    y = bundle_from_yahoo(dm.get_statements(t).data)
    if b and b["to_crore"] and y and y["fy_end"] == b["fy_end"]:
        xa, ya = f["total_assets"]["value"] * b["to_crore"], y["p"]["total_assets"]
        print(f"  total assets: XBRL {xa:,.0f} Cr vs Yahoo {ya:,.0f} Cr  -> ratio {xa / ya:.4f}  ({'OK' if abs(xa / ya - 1) < 0.01 else 'MISMATCH - check scale/parser'})")
    else:
        print("  cross-check skipped (scale unknown, or Yahoo shows a different fiscal year)")
