"""The six Shariah criteria - same thresholds, same direction of every test, same metric wording as the original engine.

New: three-valued logic. Each criterion is True (pass) / False (breach) / None (undetermined: the data does not decide it).
  verdict = NON_COMPLIANT if any criterion is False          (a breach is a breach even if other data is missing)
            COMPLIANT     if all six are True
            UNDETERMINED  otherwise - with the exact reasons
Bounds decide only when they are rigorous (e.g. total investments is an upper bound of non-compliant investments).
Nothing is assumed: unreported = unknown, no sector shortcuts, no stock-specific overrides.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .params import COMPLIANT, CRITERIA, NON_COMPLIANT, UNDETERMINED, ShariahParams


@dataclass
class Outcome:
    status: bool | None
    metric: str
    threshold: str
    note: str = ""            # why undetermined / what the number rests on


def _pos(x):
    return x is not None and x > 0


def evaluate(p: dict, to_crore: float | None, mcap_cr: float | None, evidence: dict, params: ShariahParams = ShariahParams()):
    """p: primitives (bundles.py); evidence: {field: Rs crore} already filtered to the same fiscal year. Returns (verdict, {name: Outcome})."""
    t = params
    out: dict[str, Outcome] = {}
    ta, rev = p.get("total_assets"), p.get("total_revenue")
    ta_ok, rev_ok = _pos(ta), _pos(rev)
    cr = lambda v: v * to_crore                                   # only called when to_crore is known

    # ---- C1 core business (< 5 %): needs the haram-revenue figure from the annual-report analysis / admin evidence
    th1 = f"< {t.max_haram_business_revenue_pct}%"
    hr = evidence.get("haram_business_revenue")
    if hr is not None and rev_ok and to_crore:
        r = hr / cr(rev) * 100
        out[CRITERIA[0]] = Outcome(r < t.max_haram_business_revenue_pct, f"{r:.2f}%", th1)
    else:
        why = "haram-business revenue not yet established (annual-report analysis pending)" if hr is None else (
            "total revenue not available" if not rev_ok else "statement scale unknown")
        out[CRITERIA[0]] = Outcome(None, "N/A", th1, why)

    # ---- C2 impermissible income (< 5 %): the larger of statement interest income and evidence (conflicts resolved conservatively)
    th2 = f"< {t.max_impermissible_income_pct}%"
    ratios, amounts = [], []
    if rev_ok and p.get("interest_income") is not None:
        ratios.append(p["interest_income"] / rev * 100)
        amounts.append(cr(p["interest_income"]) if to_crore else None)
    ev2 = evidence.get("impermissible_interest_income")
    if ev2 is not None and rev_ok and to_crore:
        ratios.append(ev2 / cr(rev) * 100)
        amounts.append(ev2)
    if ratios:
        i = max(range(len(ratios)), key=ratios.__getitem__)
        amt = f" (₹{amounts[i]:,.0f} Cr)" if amounts[i] is not None else ""
        out[CRITERIA[1]] = Outcome(ratios[i] < t.max_impermissible_income_pct, f"{ratios[i]:.2f}%{amt}", th2)
    else:
        out[CRITERIA[1]] = Outcome(None, "N/A", th2, "interest / impermissible income not reported" if rev_ok else "total revenue not available")

    # ---- C3 interest-bearing debt / total assets (< 33 %)
    th3 = f"< {t.max_debt_to_assets_pct}%"
    if not ta_ok:
        out[CRITERIA[2]] = Outcome(None, "N/A", th3, "total assets not available")
    elif p.get("debt") is not None:
        r = p["debt"] / ta * 100
        out[CRITERIA[2]] = Outcome(r < t.max_debt_to_assets_pct, f"{r:.2f}%", th3)
    elif p.get("debt_lb") is not None and p["debt_lb"] / ta * 100 >= t.max_debt_to_assets_pct:
        out[CRITERIA[2]] = Outcome(False, f"≥ {p['debt_lb'] / ta * 100:.2f}% (known borrowings)", th3)
    else:
        out[CRITERIA[2]] = Outcome(None, "N/A", th3, "borrowings not fully reported")

    # ---- C4 illiquid assets / total assets (>= 20 %)
    th4 = f">= {t.min_illiquid_assets_pct}%"
    if not ta_ok:
        out[CRITERIA[3]] = Outcome(None, "N/A", th4, "total assets not available")
    elif p.get("illiquid_exact") is not None:
        r = p["illiquid_exact"] / ta * 100
        out[CRITERIA[3]] = Outcome(r >= t.min_illiquid_assets_pct, f"{r:.2f}%", th4)
    elif p.get("illiquid_lb") is not None and p["illiquid_lb"] / ta * 100 >= t.min_illiquid_assets_pct:
        out[CRITERIA[3]] = Outcome(True, f"≥ {p['illiquid_lb'] / ta * 100:.2f}% (known components)", th4)
    else:
        out[CRITERIA[3]] = Outcome(None, "N/A", th4, "illiquid components not fully reported")

    # ---- C5 net liquid assets < market cap
    th5 = "Net Liquid < MCap"
    tl = p.get("total_liabilities")
    if mcap_cr is None or not mcap_cr > 0:
        out[CRITERIA[4]] = Outcome(None, "N/A", th5, "market capitalisation not available")
    elif not to_crore:
        out[CRITERIA[4]] = Outcome(None, "N/A", th5, "statement scale unknown (cannot compare with market cap)")
    elif tl is None:
        out[CRITERIA[4]] = Outcome(None, "N/A", th5, "total liabilities not reported")
    else:
        lo, hi, ex = p.get("liquid_lo"), p.get("liquid_hi"), p.get("liquid_exact")
        if ex is not None:
            nl = cr(ex - tl)
            out[CRITERIA[4]] = Outcome(nl < mcap_cr, f"Net Liq: ₹{nl:,.0f} Cr | MCap: ₹{mcap_cr:,.0f} Cr", th5)
        elif hi is not None and cr(hi - tl) < mcap_cr:
            out[CRITERIA[4]] = Outcome(True, f"Net Liq ≤ ₹{cr(hi - tl):,.0f} Cr (upper bound) | MCap: ₹{mcap_cr:,.0f} Cr", th5)
        elif lo is not None and cr(lo - tl) >= mcap_cr:
            out[CRITERIA[4]] = Outcome(False, f"Net Liq ≥ ₹{cr(lo - tl):,.0f} Cr (known liquid assets) | MCap: ₹{mcap_cr:,.0f} Cr", th5)
        else:
            out[CRITERIA[4]] = Outcome(None, "N/A", th5, "liquid assets not fully reported")

    # ---- C6 non-compliant investments / total assets (< 33 %): exact evidence, else total investments as a rigorous upper bound
    th6 = f"< {t.max_non_compliant_inv_pct}%"
    nci = evidence.get("non_compliant_investments")
    if not ta_ok:
        out[CRITERIA[5]] = Outcome(None, "N/A", th6, "total assets not available")
    elif nci is not None and to_crore:
        r = nci / cr(ta) * 100
        out[CRITERIA[5]] = Outcome(r < t.max_non_compliant_inv_pct, f"{r:.2f}% (₹{nci:,.0f} Cr)", th6)
    elif p.get("inv_ub") is not None and p["inv_ub"] / ta * 100 < t.max_non_compliant_inv_pct:
        out[CRITERIA[5]] = Outcome(True, f"≤ {p['inv_ub'] / ta * 100:.2f}% (upper bound: all investments)", th6)
    else:
        out[CRITERIA[5]] = Outcome(None, "N/A", th6, "investment classification pending (annual-report analysis)")

    sts = [o.status for o in out.values()]
    verdict = NON_COMPLIANT if any(s is False for s in sts) else (COMPLIANT if all(s is True for s in sts) else UNDETERMINED)
    return verdict, out
