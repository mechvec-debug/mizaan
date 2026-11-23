"""Quality Value Rebound (ex "Deep-Value Sentinel") - every threshold of the ORIGINAL scanner, unchanged.

Preservation note: the original source comments say the PASS zone is "50-70% below the high" and WATCH is "fall >= 40%",
but the CODE values are DD 35-65 and WATCH 25. The code values are the business rule and are kept here.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class FundamentalParams:
    # 1) fundamentals (strong business)
    min_mcap_cr: float = 1000
    min_sales_growth: float = 8
    min_opm: float = 8
    min_roe: float = 10
    min_roce: float = 10
    max_debt_equity: float = 1.0          # ignored for Financial Services
    # 2) valuation
    max_pe: float = 35
    max_peg: float = 2.0
    # 3) price fall from the reference high
    high_lookback_years: int = 3
    dd_min: float = 35
    dd_max: float = 65
    watch_min_dd: float = 25
    # 4) bottom + 5) daily upward strength
    max_bounce_from_low: float = 35
    min_tech_score: int = 4
    min_rr: float = 1.5
    # data handling (new - not part of the original business rules)
    min_bars: int = 120                   # original: fewer than 120 daily bars -> ticker skipped
    carry_forward_days: int = 7           # keep a ticker's previous row this long when today's data is unavailable


STATUS_ORDER = {"🔥 PASS": 0, "👀 WATCH": 1, "WAIT (Price)": 2, "WAIT (Valuation)": 3, "WAIT (Fund)": 4}
OUTPUT_COLUMNS = ["Ticker", "Market Cap", "Current price", "PE", "OPM", "Sales growth", "ROE", "ROCE", "EPS current", "Volume",
                  "RSI", "Status", "Sector", "Drawdown %", "Above 52W Low %", "Higher Low", "Tech Score", "Stop Loss",
                  "Target 1", "Target 2", "Risk Reward", "Notes", "Updated"]
