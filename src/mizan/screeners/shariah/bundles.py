"""Turn a data source (XBRL filing or Yahoo statements) into ONE common set of primitives for the criteria engine.

Rules (strict, no assumptions):
  * a value that is not reported is None (unknown) - never 0
  * where an exact figure is unknown but a bound is known the bound is kept (lower / upper) because a bound can still decide a criterion
  * one bundle = ONE source and ONE fiscal year; fields are never mixed across sources or years
  * `to_crore` converts bundle units to Rs crore; None = unknown (only Criterion 5 and rupee evidence need it - ratios are scale-free)
Canonical definitions (same for both sources):
  illiquid  = PPE + CWIP + investment property + intangibles + inventories
  liquid    = cash & equivalents + bank balances + trade receivables + other current financial assets
  debt      = borrowings (XBRL: current + non-current)   |   Yahoo "Total Debt"
"""
from __future__ import annotations

import pandas as pd

CR = 1e7


def _sum(vals):
    k = [v for v in vals if v is not None]
    return (sum(k) if k else None), len(k) == len(vals)


def _primitives(f: dict) -> dict:
    """f: canonical component dict (None = unknown). Returns the criteria primitives."""
    illiquid_lb, ill_all = _sum([f.get(k) for k in ("ppe", "cwip", "investment_property", "intangible", "inventories")])
    liquid_lo, liq_all = _sum([f.get(k) for k in ("cash_equivalents", "bank_balances", "trade_receivables", "other_current_financial_assets")])
    debt_lb, debt_all = _sum([f.get("current_borrowings"), f.get("non_current_borrowings")])
    inv, inv_all = _sum([f.get("current_investments"), f.get("noncurrent_investments")])
    return {
        "total_assets": f.get("total_assets"), "total_revenue": f.get("total_revenue"), "total_liabilities": f.get("total_liabilities"),
        "debt": f["debt"] if "debt" in f else (debt_lb if debt_all else None), "debt_lb": f["debt"] if f.get("debt") is not None else debt_lb,
        "illiquid_lb": illiquid_lb, "illiquid_exact": illiquid_lb if ill_all else None,
        "liquid_lo": liquid_lo, "liquid_exact": liquid_lo if liq_all else None,
        "liquid_hi": liquid_lo if liq_all else f.get("current_assets"),
        "interest_income": f.get("interest_income"),
        "inv_ub": inv if inv_all else None,
    }


def bundle_from_xbrl(x: dict) -> dict | None:
    """x = output of xbrl.parse_xbrl_facts. None when the filing cannot be used (no FY end / not annual / no assets)."""
    if not x or not x.get("fy_end") or not x.get("annual"):
        return None
    f = {k: v["value"] for k, v in x["facts"].items()}
    if f.get("total_assets") is None:
        return None
    sc = x.get("scale")
    return {"source": "xbrl", "fy_end": x["fy_end"], "to_crore": (sc["to_rupees"] / CR) if sc else None,
            "scale": (sc or {}).get("text"), "p": _primitives(f), "tags": {k: v["tag"] for k, v in x["facts"].items()}}


def _row(df, names, col):
    if df is None or getattr(df, "empty", True) or col not in df.columns:
        return None, None
    for n in names:
        if n in df.index:
            v = df.loc[n]
            v = v.iloc[0] if isinstance(v, pd.DataFrame) else v
            v = v[col] if isinstance(v, pd.Series) else v
            if pd.notna(v):
                return float(v) / CR, n
    return None, None


def bundle_from_yahoo(statements: dict) -> dict | None:
    """Annual statements from the shared cache. Every value is read from the SAME fiscal-year column (latest balance sheet)."""
    bs = (statements or {}).get("balance_sheet")
    pnl = (statements or {}).get("income_stmt")
    if pnl is None or getattr(pnl, "empty", True):
        pnl = (statements or {}).get("financials")
    if bs is None or getattr(bs, "empty", True):
        return None
    col = max(bs.columns)
    tags, f = {}, {}

    def get(key, df, names):
        v, n = _row(df, names, col)
        if n:
            tags[key] = n
        return v

    f["total_assets"] = get("total_assets", bs, ["Total Assets"])
    f["total_liabilities"] = get("total_liabilities", bs, ["Total Liabilities Net Minority Interest"])
    if f["total_liabilities"] is None:                      # accounting identity, only if both pieces are reported
        eq = get("total_equity", bs, ["Total Equity Gross Minority Interest"])
        if f["total_assets"] is not None and eq is not None:
            f["total_liabilities"], tags["total_liabilities"] = f["total_assets"] - eq, "Total Assets - Total Equity Gross Minority Interest"
    f["total_revenue"] = get("total_revenue", pnl, ["Total Revenue"])
    f["debt"] = get("debt", bs, ["Total Debt"])
    f["ppe"] = get("ppe", bs, ["Net PPE"])
    f["investment_property"] = get("investment_property", bs, ["Investment Properties"])
    f["inventories"] = get("inventories", bs, ["Inventory"])
    intang = get("intangible", bs, ["Goodwill And Other Intangible Assets"])      # aggregate row; never add its components on top
    if intang is None:
        gw, _ = _row(bs, ["Goodwill"], col)
        oi, _ = _row(bs, ["Other Intangible Assets"], col)
        intang = (gw or 0.0) + (oi or 0.0) if (gw is not None or oi is not None) else None
    f["intangible"] = intang
    f["cwip"] = None                                                                # Yahoo folds CWIP into Net PPE: not separately known
    cash = get("cash_sti", bs, ["Cash Cash Equivalents And Short Term Investments"])
    rec = get("receivables", bs, ["Receivables", "Accounts Receivable"])
    cur = get("current_assets", bs, ["Current Assets"])
    f["interest_income"] = get("interest_income", pnl, ["Interest Income", "Interest Income Non Operating"])   # gross rows only
    sti = get("other_sti", bs, ["Other Short Term Investments"])
    lti = get("investments_and_advances", bs, ["Investments And Advances"])

    p = _primitives({**f, "debt": f["debt"]})
    # Yahoo's illiquid set has no separate CWIP / bank-balance rows: only the rows it really has decide "all known"
    k = [f["ppe"], f["investment_property"], f["inventories"], f["intangible"]]
    p["illiquid_lb"], p["illiquid_exact"] = (sum(v for v in k if v is not None) if any(v is not None for v in k) else None), (sum(k) if all(v is not None for v in k) else None)
    lk = [v for v in (cash, rec) if v is not None]
    p["liquid_lo"], p["liquid_exact"], p["liquid_hi"] = (sum(lk) if lk else None), None, cur
    p["inv_ub"] = (sti + lti) if (sti is not None and lti is not None) else None
    return {"source": "yahoo", "fy_end": pd.Timestamp(col).date().isoformat(), "to_crore": 1.0, "scale": "crore (converted)",
            "p": p, "tags": tags}
