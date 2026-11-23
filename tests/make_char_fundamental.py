"""Generate characterization fixtures from the ORIGINAL Fundamental scanner (scan_ticker / technicals)."""
import json, os, sys
sys.path.insert(0, os.path.dirname(__file__))
import harness
import numpy as np
import pandas as pd

fw = harness.load_fundamental()
OUT = os.path.join(harness.ROOT, "golden_master", "characterization")


def path(knots, n, seed, noise=0.012, vol_recent=1.6):
    """Piecewise-linear log-price through (frac, price) knots + seeded noise. Returns OHLCV DataFrame."""
    rng = np.random.default_rng(seed)
    xs = np.array([k[0] for k in knots]) * (n - 1)
    ys = np.log([k[1] for k in knots])
    base = np.exp(np.interp(np.arange(n), xs, ys) + rng.normal(0, noise, n).cumsum() * 0.25)
    close = base
    high = close * (1 + np.abs(rng.normal(0, 0.008, n)))
    low = close * (1 - np.abs(rng.normal(0, 0.008, n)))
    vol = rng.integers(80_000, 120_000, n).astype(float)
    vol[-12:] = np.round(vol[-12:] * vol_recent)          # whole shares, as in real Yahoo data
    idx = pd.bdate_range("2023-11-01", periods=n)
    r4 = lambda a: np.round(a, 4)          # rounded at creation so saved arrays reproduce results exactly
    return pd.DataFrame({"Open": r4(close), "High": r4(high), "Low": r4(low), "Close": r4(close), "Volume": vol}, index=idx)


GOOD_INFO = dict(sector="Industrials", marketCap=5000e7, revenueGrowth=0.18, operatingMargins=0.15, returnOnEquity=0.18,
                 debtToEquity=40.0, trailingPE=18.0, trailingEps=12.0, earningsGrowth=0.2, pegRatio=1.2)


from fixture_builders import fin


def run(name, hist, info, income_balance="default"):
    """income_balance: "default" | None (statements missing) | dict of fin() kwargs"""
    if income_balance == "default":
        inc, bal = fin()
    elif income_balance is None:
        inc, bal = None, None
    else:
        inc, bal = fin(**income_balance)
    sym = f"{name}.NS"
    harness.FakeTicker.registry[sym] = harness.FakeTicker(sym, info=info, hist=hist, income=inc, balance=bal)
    row = fw.scan_ticker(sym)
    if row:
        row = {k: v for k, v in row.items() if k != "Updated"}   # wall-clock field, excluded from parity
    return row


def pack(hist):
    return {c: [round(float(x), 4) for x in hist[c].values] for c in ["Open", "High", "Low", "Close", "Volume"]} | \
           {"dates": [d.strftime("%Y-%m-%d") for d in hist.index]}


# --- price-shape library (fractions of the 3y window, price level) ---
SHAPES = {
    # long rise, ~52% fall, base, higher low, gentle recovery  -> candidate for PASS
    "bottoming": [(0, 60), (.35, 100), (.72, 47), (.82, 46.5), (.9, 49.5), (1, 55.5)],
    "bottoming_b": [(0, 70), (.30, 100), (.70, 50), (.80, 48), (.90, 53), (1, 59)],
    "falling_knife": [(0, 80), (.3, 100), (1, 52)],              # fall 48%, still falling -> WATCH
    "near_high": [(0, 60), (.5, 85), (1, 98)],                    # tiny drawdown -> WAIT (Price)
    "mild_pullback": [(0, 60), (.6, 100), (1, 85)],               # dd 15% -> WAIT (Price)
}

import itertools
def rally_shapes():
    for low_f, top, end in itertools.product([.70,.76,.80], [58,62,66], [54,56,58]):
        yield (low_f, top, end), [(0,60),(.35,100),(low_f,46),(low_f+.06,46.8),(.93,top),(1,end)]

results = {}
status_found = {}
for key, knots in rally_shapes():
    for seed in range(40):
        h = path(knots, 750, seed)
        try: row = run("PASSC", h, dict(GOOD_INFO))
        except Exception: continue
        if row and row["Status"] == "🔥 PASS":
            SHAPES["rally_bottom"] = knots; status_found["PASS"] = ("rally_bottom", seed); break
    if "PASS" in status_found: break

scen = []
# search seeds for a PASS and a WATCH under the bottoming shapes
for shape in ["bottoming", "bottoming_b"]:
    for seed in range(400):
        h = path(SHAPES[shape], 750, seed)
        try:
            row = run("PASSC", h, dict(GOOD_INFO))
        except Exception:
            continue
        if row and row["Status"] == "👀 WATCH" and "WATCH" not in status_found:
            status_found["WATCH"] = (shape, seed)
        if "WATCH" in status_found:
            break
    if "WATCH" in status_found:
        break
print("search:", status_found)

def add(name, hist, info, ib="default", note=""):
    row = run(name, hist, info, ib)
    scen.append({"name": name, "note": note, "info": info, "hist": pack(hist) if hist is not None else None,
                 "income_balance": ib,
                 "expected_row": row})
    print(f"  {name:<28} -> {row['Status'] if row else 'None(skipped)'}")

if "PASS" in status_found:
    s, sd = status_found["PASS"]; add("pass_deep_value", path(SHAPES[s], 750, sd), dict(GOOD_INFO), note=f"{s}/seed{sd}")
if "WATCH" in status_found:
    s, sd = status_found["WATCH"]; add("watch_tech_missing", path(SHAPES[s], 750, sd), dict(GOOD_INFO), note=f"{s}/seed{sd}")
add("watch_falling_knife", path(SHAPES["falling_knife"], 750, 7), dict(GOOD_INFO))
add("wait_price_near_high", path(SHAPES["near_high"], 750, 3), dict(GOOD_INFO))
add("wait_price_mild_pullback", path(SHAPES["mild_pullback"], 750, 4), dict(GOOD_INFO))
add("wait_valuation_pe_80", path(SHAPES["falling_knife"], 750, 7), dict(GOOD_INFO, trailingPE=80.0))
add("wait_valuation_peg_3", path(SHAPES["falling_knife"], 750, 7), dict(GOOD_INFO, pegRatio=3.0))
add("peg_unknown_not_penalised", path(SHAPES["falling_knife"], 750, 7), dict(GOOD_INFO, pegRatio=None, trailingPegRatio=None))
add("pe_from_forward_when_trailing_zero", path(SHAPES["falling_knife"], 750, 7), dict(GOOD_INFO, trailingPE=0, forwardPE=22.0))
add("wait_fund_low_roe", path(SHAPES["falling_knife"], 750, 7), dict(GOOD_INFO), ib=dict(ni=20e7))
add("wait_fund_small_mcap", path(SHAPES["falling_knife"], 750, 7), dict(GOOD_INFO, marketCap=900e7))
add("financial_sector_debt_ignored", path(SHAPES["falling_knife"], 750, 7), dict(GOOD_INFO, sector="Financial Services", debtToEquity=900.0))
add("non_financial_debt_fail", path(SHAPES["falling_knife"], 750, 7), dict(GOOD_INFO, debtToEquity=150.0))
add("statements_missing_fallbacks", path(SHAPES["falling_knife"], 750, 7), dict(GOOD_INFO, returnOnEquity=0.2), ib=None, note="no statements: ROCE becomes 0 => always fails ROCE in original")
add("eps_below_prior_year_fails", path(SHAPES["falling_knife"], 750, 7), dict(GOOD_INFO, trailingEps=8.0), ib=dict(eps=(9.0, 8.0)), note="trailingEps 8 < latest annual diluted EPS 9")
add("short_history_119_bars_skipped", path(SHAPES["falling_knife"], 119, 7), dict(GOOD_INFO))
add("history_exactly_120_bars", path(SHAPES["falling_knife"], 120, 7), dict(GOOD_INFO))

# pure technicals() characterization (the daily-indicator maths)
tech = []
for name, shape, sd in [("bottoming", "bottoming", 1), ("falling_knife", "falling_knife", 2), ("near_high", "near_high", 3)]:
    h = path(SHAPES[shape], 750, sd)
    t = fw.technicals(h)
    tech.append({"name": name, "hist": pack(h), "expected": {k: (float(v) if not isinstance(v, (bool, np.bool_)) else bool(v)) for k, v in t.items()}})

json.dump({"scenarios": scen}, open(os.path.join(OUT, "fundamental_scan_ticker_cases.json"), "w"), ensure_ascii=False)
json.dump({"cases": tech}, open(os.path.join(OUT, "fundamental_technicals_cases.json"), "w"), ensure_ascii=False)
print("statuses reached:", sorted({s['expected_row']['Status'] for s in scen if s['expected_row']}))
