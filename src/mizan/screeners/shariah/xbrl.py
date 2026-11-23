"""Period-aware XBRL (Ind-AS) fact extraction.

Fixes vs the original parser (all documented in docs/SHARIAH.md):
  * values are taken from the CURRENT fiscal-year context (instant == FY end / duration ending at FY end, no dimensions),
    not "the last numeric node in the file" (which could be a prior year or a segment)
  * `total_liabilities` uses the Liabilities tag (the original took EquityAndLiabilities first = total assets)
  * the unit comes from the filing's own scale element; if it is absent the scale is UNKNOWN (None) - we never guess from
    "value > 1e7". Ratios are scale-free; only the absolute comparison in Criterion 5 needs the scale.
  * the filing period length is returned so quarterly / half-yearly filings can be rejected (`annual`)
Facts are matched by local tag name, case-insensitive, like the original.
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from lxml import etree

INSTANT_TAGS = {
    "total_assets": ["Assets", "TotalAssets"],
    "total_liabilities": ["Liabilities", "TotalLiabilities"],
    "equity_and_liabilities": ["EquityAndLiabilities", "TotalEquityAndLiabilities"],
    "total_equity": ["Equity", "TotalEquity"],
    "ppe": ["PropertyPlantAndEquipment", "TangibleAssets"],
    "cwip": ["CapitalWorkInProgress"],
    "investment_property": ["InvestmentProperty"],
    "intangible": ["IntangibleAssets", "OtherIntangibleAssets"],
    "inventories": ["Inventories"],
    "cash_equivalents": ["CashAndCashEquivalents"],
    "bank_balances": ["OtherBalancesWithBanks", "BankBalancesOtherThanCashAndCashEquivalents"],
    "trade_receivables": ["TradeReceivablesCurrent", "CurrentTradeReceivables"],
    "other_current_financial_assets": ["OtherCurrentFinancialAssets"],
    "current_assets": ["CurrentAssets", "AssetsCurrent"],
    "current_borrowings": ["BorrowingsCurrent", "CurrentBorrowings"],
    "non_current_borrowings": ["BorrowingsNonCurrent", "NonCurrentBorrowings"],
    "current_investments": ["CurrentInvestments", "InvestmentsCurrent"],
    "noncurrent_investments": ["NonCurrentInvestments", "InvestmentsNoncurrent", "InvestmentsNonCurrent"],
}
DURATION_TAGS = {
    "total_revenue": ["RevenueFromOperations", "Income"],
    "interest_income": ["InterestIncome", "InterestEarned"],                       # totals (banks/NBFCs report InterestEarned)
    "interest_income_parts": ["InterestIncomeFromBankDeposits", "InterestIncomeOthers"],  # summed only if no total exists
}
SCALES = {"actual": 1.0, "rupees": 1.0, "inr": 1.0, "units": 1.0, "thousands": 1e3, "thousand": 1e3, "lakhs": 1e5, "lakh": 1e5,
          "lacs": 1e5, "millions": 1e6, "million": 1e6, "crores": 1e7, "crore": 1e7, "cr": 1e7}
ANNUAL_MIN_DAYS = 330


def _d(s):
    try:
        return datetime.strptime((s or "").strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _local(el) -> str:
    return etree.QName(el).localname.lower() if isinstance(el.tag, str) else ""


def parse_xbrl_facts(source) -> dict:
    """source: path or bytes. Returns {fy_end, period_days, annual, scale, facts{metric:{value,tag}}} (values in filing units)."""
    root = etree.fromstring(source) if isinstance(source, (bytes, bytearray)) else etree.parse(str(source)).getroot()

    ctx = {}
    for c in root.iter():
        if _local(c) != "context":
            continue
        inst = start = end = None
        dim = False
        for e in c.iter():
            n = _local(e)
            if n == "instant":
                inst = _d(e.text)
            elif n == "startdate":
                start = _d(e.text)
            elif n == "enddate":
                end = _d(e.text)
            elif n in ("explicitmember", "typedmember"):
                dim = True
        ctx[c.get("id")] = {"instant": inst, "start": start, "end": end, "dim": dim}

    facts: dict[str, list] = {}
    for e in root.iter():
        ref = e.get("contextRef") if isinstance(e.tag, str) else None
        if ref is None or ref not in ctx or ctx[ref]["dim"] or e.get("{http://www.w3.org/2001/XMLSchema-instance}nil") == "true":
            continue
        try:
            v = float((e.text or "").strip())
        except ValueError:
            continue
        facts.setdefault(_local(e), []).append((v, ctx[ref]))

    def find(tags, kind, fy=None, days=None):
        for tag in tags:
            hits = facts.get(tag.lower(), [])
            if kind == "instant":
                hits = [(v, c) for v, c in hits if c["instant"] and (fy is None or c["instant"] == fy)]
                if hits:
                    return (max(hits, key=lambda h: h[1]["instant"]) if fy is None else hits[-1]), tag
            else:
                hits = [(v, c) for v, c in hits if c["start"] and c["end"] and (fy is None or c["end"] == fy)]
                if days is not None:
                    hits = [(v, c) for v, c in hits if (c["end"] - c["start"]).days == days]
                if hits:
                    return max(hits, key=lambda h: (h[1]["end"], (h[1]["end"] - h[1]["start"]).days)), tag
        return None, None

    # fiscal-year end = latest balance-sheet date of total assets (fallback: latest context date of any fact)
    hit, _ = find(INSTANT_TAGS["total_assets"], "instant")
    if hit:
        fy_end = hit[1]["instant"]
    else:
        dates = [c["instant"] or c["end"] for lst in facts.values() for _, c in lst if (c["instant"] or c["end"])]
        fy_end = max(dates) if dates else None

    period_days = None
    if fy_end:
        rev, _ = find(DURATION_TAGS["total_revenue"], "duration", fy=fy_end)
        if rev:
            period_days = (rev[1]["end"] - rev[1]["start"]).days

    out: dict = {}
    for metric, tags in INSTANT_TAGS.items():
        hit, tag = find(tags, "instant", fy=fy_end)
        if hit:
            out[metric] = {"value": hit[0], "tag": tag}
    for metric, tags in DURATION_TAGS.items():
        if metric == "interest_income_parts":
            continue
        hit, tag = find(tags, "duration", fy=fy_end, days=period_days)
        if hit:
            out[metric] = {"value": hit[0], "tag": tag}
    if "interest_income" not in out:                         # no total reported: sum the component tags that exist
        parts = [find([t], "duration", fy=fy_end, days=period_days) for t in DURATION_TAGS["interest_income_parts"]]
        parts = [(h[0], t) for h, t in parts if h]
        if parts:
            out["interest_income"] = {"value": sum(v for v, _ in parts), "tag": "+".join(t for _, t in parts)}
    if "total_liabilities" not in out and "equity_and_liabilities" in out and "total_equity" in out:
        out["total_liabilities"] = {"value": out["equity_and_liabilities"]["value"] - out["total_equity"]["value"],
                                    "tag": "EquityAndLiabilities-Equity"}

    scale = None
    for e in root.iter():
        n = _local(e)
        if n.startswith("levelofrounding") or "scaleoffigure" in n:
            txt = (e.text or "").strip().lower()
            if txt in SCALES:
                scale = {"text": txt, "to_rupees": SCALES[txt]}
            break
    return {"fy_end": fy_end.isoformat() if fy_end else None, "period_days": period_days,
            "annual": bool(period_days and period_days >= ANNUAL_MIN_DAYS), "scale": scale, "facts": out}
