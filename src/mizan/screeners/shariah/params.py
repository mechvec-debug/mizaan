"""Shariah Compliance Screener - the six criteria and thresholds, exactly as in the original constants.py.

STRICT POLICY (your instruction): no assumption, no exception. A criterion is decided only when the data decides it; otherwise
its status is None ("undetermined") with the reason - never a silent 0, never a sector shortcut, never a stock-specific override.
"""
from dataclasses import dataclass

CRITERIA = ["Criteria 1: Core Business", "Criteria 2: Impermissible Income", "Criteria 3: Debt / Assets",
            "Criteria 4: Illiquid / Assets", "Criteria 5: Net Liquid < MCap", "Criteria 6: Non-Compliant Inv"]

COMPLIANT, NON_COMPLIANT, UNDETERMINED = "COMPLIANT", "NON_COMPLIANT", "UNDETERMINED"


@dataclass(frozen=True)
class ShariahParams:
    max_haram_business_revenue_pct: float = 5.0      # C1  haram revenue / total revenue
    max_impermissible_income_pct: float = 5.0        # C2  impermissible income / total revenue
    max_debt_to_assets_pct: float = 33.0             # C3  interest-bearing debt / total assets
    min_illiquid_assets_pct: float = 20.0            # C4  illiquid assets / total assets  (>=)
    max_non_compliant_inv_pct: float = 33.0          # C6  non-compliant investments / total assets
    # C5: (liquid assets - total liabilities) must be LESS than market capitalisation
    carry_forward_days: int = 30                     # annual screen: keep the previous verdict this long if data is unavailable today
