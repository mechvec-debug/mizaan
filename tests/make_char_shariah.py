"""Generate characterization fixtures from the ORIGINAL Shariah code (engine + XBRL parser)."""
import json, os, sys, tempfile
sys.path.insert(0, os.path.dirname(__file__))
import harness
import pandas as pd

const, engine, xp = harness.load_shariah()
OUT = os.path.join(harness.ROOT, "golden_master", "characterization")


def case(name, mcap, fin, audit):
    return {"name": name, "market_cap": mcap, "financials": fin, "audit": audit}


BASE = dict(total_assets=1000.0, total_revenue=1000.0, total_liabilities=500.0, interest_bearing_debt=100.0,
            illiquid_assets=400.0, liquid_assets=300.0, impermissible_interest_income=10.0, non_compliant_investments=50.0)
A0 = dict(haram_business_revenue=0.0, impermissible_interest_income=10.0, non_compliant_investments=50.0)


def v(**kw):
    d = dict(BASE); d.update(kw); return d


cases = [
    case("all_pass", 5000, v(), A0),
    # exact boundaries: C1/C2 '<5', C3 '<33', C4 '>=20', C6 '<33'
    case("c1_exactly_5pct_fails", 5000, v(), dict(A0, haram_business_revenue=50.0)),
    case("c1_just_below_5pct_passes", 5000, v(), dict(A0, haram_business_revenue=49.99)),
    case("c2_exactly_5pct_fails", 5000, v(), dict(A0, impermissible_interest_income=50.0)),
    case("c2_from_financials_when_audit_zero", 5000, v(impermissible_interest_income=60.0), dict(A0, impermissible_interest_income=0.0)),
    case("c3_exactly_33pct_fails", 5000, v(interest_bearing_debt=330.0), A0),
    case("c3_just_below_33pct_passes", 5000, v(interest_bearing_debt=329.9), A0),
    case("c4_exactly_20pct_passes", 5000, v(illiquid_assets=200.0), A0),
    case("c4_just_below_20pct_fails", 5000, v(illiquid_assets=199.9), A0),
    case("c5_net_liquid_equals_mcap_fails", 5000, v(liquid_assets=5500.0, total_liabilities=500.0), A0),
    case("c5_net_liquid_below_mcap_passes", 5000, v(liquid_assets=5499.0, total_liabilities=500.0), A0),
    case("c5_negative_net_liquid_passes", 100, v(liquid_assets=0.0, total_liabilities=500.0), A0),
    case("c6_exactly_33pct_fails", 5000, v(), dict(A0, non_compliant_investments=330.0)),
    case("c6_from_financials_when_audit_zero", 5000, v(non_compliant_investments=400.0), dict(A0, non_compliant_investments=0.0)),
    case("guard_zero_assets", 5000, v(total_assets=0.0), A0),
    case("guard_zero_revenue", 5000, v(total_revenue=0.0), A0),
    case("guard_nan_assets", 5000, v(total_assets=float("nan")), A0),
    case("guard_none_values_default_zero", 5000, {"total_assets": 1000.0, "total_revenue": 1000.0}, {}),
    case("all_fail", 10, v(interest_bearing_debt=900.0, illiquid_assets=10.0, liquid_assets=9000.0), dict(haram_business_revenue=900.0, impermissible_interest_income=900.0, non_compliant_investments=900.0)),
]
engine_out = []
for c in cases:
    r = engine.evaluate_all_criteria(c["market_cap"], c["financials"], c["audit"])
    engine_out.append({**c, "expected": r})
json.dump(engine_out, open(os.path.join(OUT, "shariah_engine_cases.json"), "w", indent=None) if False else open(os.path.join(OUT, "shariah_engine_cases.json"), "w"), indent=1, ensure_ascii=False, default=lambda o: "NaN")

# ---- XBRL parser characterization (local XML only; no ticker => no Yahoo fallback) ----
from fixture_builders import xbrl


FILES = {
    "xbrl_crore_scale": [("Assets", 1000), ("RevenueFromOperations", 800), ("EquityAndLiabilities", 1000), ("PropertyPlantAndEquipment", 300),
                          ("Inventories", 100), ("CashAndCashEquivalents", 50), ("TradeReceivablesCurrent", 60), ("BorrowingsCurrent", 20),
                          ("BorrowingsNonCurrent", 80), ("InterestIncome", 12), ("CurrentInvestments", 40)],
    "xbrl_rupee_scale_gt_1e7": [("Assets", 10000000000), ("RevenueFromOperations", 8000000000), ("EquityAndLiabilities", 10000000000),
                                 ("PropertyPlantAndEquipment", 3000000000), ("BorrowingsCurrent", 200000000)],
    "xbrl_value_exactly_1e7_not_scaled": [("Assets", 10000000), ("RevenueFromOperations", 10000000)],
    "xbrl_liabilities_tag_priority": [("Assets", 1000), ("RevenueFromOperations", 500), ("EquityAndLiabilities", 1000), ("Liabilities", 400)],
    "xbrl_empty": [],
}
parser_out = {}
tmp = tempfile.mkdtemp()
for name, tags in FILES.items():
    p = os.path.join(tmp, name + ".xml"); open(p, "w").write(xbrl(tags))
    parser_out[name] = {"tags": tags, "expected": xp.parse_xbrl_primitives(p)}

# ---- Yahoo fallback path via fake yfinance ----
idx = ["Total Assets", "Total Debt", "Net PPE", "Inventory", "Goodwill", "Cash Cash Equivalents And Short Term Investments",
       "Total Liabilities Net Minority Interest", "Other Short Term Investments"]
bs = pd.DataFrame({pd.Timestamp("2026-03-31"): [2e10, 4e9, 6e9, 1e9, 5e8, 3e9, 9e9, 7e8]}, index=idx)
pnl = pd.DataFrame({pd.Timestamp("2026-03-31"): [1.5e10, 2e8]}, index=["Total Revenue", "Interest Income"])
harness.FakeTicker.registry["FAKE.NS"] = harness.FakeTicker("FAKE.NS", balance=bs, financials=pnl)
parser_out["yahoo_fallback_FAKE"] = {"note": "balance_sheet/financials values in rupees; expected in crore",
                                      "expected": xp.parse_xbrl_primitives(None, ticker_symbol="FAKE")}
json.dump(parser_out, open(os.path.join(OUT, "shariah_xbrl_parser_cases.json"), "w"), indent=1, ensure_ascii=False)
print("engine cases:", len(engine_out), "| parser cases:", len(parser_out))
for c in engine_out:
    print(f"  {c['name']:<42} compliant={c['expected']['is_compliant']}")
print(json.dumps(parser_out["xbrl_liabilities_tag_priority"]["expected"]))
print(json.dumps(parser_out["xbrl_value_exactly_1e7_not_scaled"]["expected"]))
