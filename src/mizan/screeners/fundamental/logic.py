"""Quality Value Rebound - calculation logic.

A VERBATIM port of the original worker's helpers, `technicals` and `scan_ticker`. Only two things changed:
  * thresholds come from `FundamentalParams` instead of module constants
  * no network, retry, sleep or Google Sheet code: the data arrives as arguments (the Data Manager supplies it)
Do not "tidy" the arithmetic - parity with the original is proven by differential tests (tests/unit/test_fundamental.py).
"""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd

from .params import FundamentalParams


# ---------------------- helpers (unchanged) ----------------------
def num(x, default=0.0):
    """Convert anything to a finite float (None / NaN / inf -> default)."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return default if (np.isnan(v) or np.isinf(v)) else v


def rnd(x, n=2):
    return round(num(x), n)


def annual_values(df, keys):
    """Newest-first list of non-null annual values for the first matching row."""
    if df is None or df.empty:
        return []
    for key in keys:
        if key in df.index:
            row = df.loc[key]
            if isinstance(row, pd.DataFrame):
                row = row.iloc[0]
            row = row.dropna()
            if not row.empty:
                return [float(v) for v in row.sort_index(ascending=False).values]
    return []


def latest(df, keys):
    vals = annual_values(df, keys)
    return vals[0] if vals else 0.0


# ---------------------- indicators (unchanged) ----------------------
def rsi_wilder(close, period=14):
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - 100 / (1 + rs)
    rsi = rsi.where(loss != 0, 100.0)
    return rsi.fillna(50.0)


def technicals(hist, params: FundamentalParams = FundamentalParams()):
    """All daily-timeframe calculations. `hist` needs >= 120 daily bars."""
    close, high, low, vol = hist["Close"], hist["High"], hist["Low"], hist["Volume"]
    cp = close.iloc[-1]

    ema20 = close.ewm(span=20, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()
    macd = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    macd_sig = macd.ewm(span=9, adjust=False).mean()
    macd_hist = macd - macd_sig
    rsi = rsi_wilder(close)

    prev_close = close.shift()
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    atr = num(tr.rolling(14).mean().iloc[-1])

    low_52w = low.iloc[-252:].min()
    high_ref = high.max()                       # highest High in lookback window
    drawdown = (1 - cp / high_ref) * 100        # % below reference high
    bounce = (cp / low_52w - 1) * 100           # % above 52-week low

    # Bottom: price still near the low, and the latest swing low is HIGHER than the old one
    recent_low = low.iloc[-15:].min()
    prior_low = low.iloc[-90:-15].min()
    higher_low = recent_low > prior_low
    near_bottom = bounce <= params.max_bounce_from_low

    # Upward strength checks (daily)
    c_ema = cp > ema20.iloc[-1] and ema20.iloc[-1] > ema20.iloc[-6]       # above rising 20 EMA
    c_macd = macd.iloc[-1] > macd_sig.iloc[-1] and macd_hist.iloc[-1] >= macd_hist.iloc[-4]
    c_rsi = 45 <= rsi.iloc[-1] <= 70 and rsi.iloc[-1] > rsi.iloc[-6]       # RSI healthy and rising
    c_vol = vol.iloc[-10:].mean() >= vol.iloc[-50:].mean()                 # volume picking up
    c_ema50 = cp > ema50.iloc[-1]                                          # reclaimed 50 EMA
    checks = [c_ema, c_macd, c_rsi, c_vol, c_ema50]
    tech_score = int(sum(bool(c) for c in checks))
    momentum_ok = bool(c_ema and c_macd and tech_score >= params.min_tech_score)

    # Trade plan
    stop = recent_low * 0.98
    risk = cp - stop
    t1 = max(high.iloc[-60:].max(), cp + 2 * atr)
    t2 = max(t1, low_52w + 0.5 * (high_ref - low_52w))   # 50% retracement of the fall
    rr = (t1 - cp) / risk if risk > 0 else 0.0

    return dict(cp=cp, rsi=rsi.iloc[-1], volume=vol.iloc[-1], drawdown=drawdown, bounce=bounce,
                higher_low=bool(higher_low), near_bottom=bool(near_bottom),
                tech_score=tech_score, momentum_ok=momentum_ok,
                stop=stop, t1=t1, t2=t2, rr=rr)


# ---------------------- per-ticker evaluation (original scan_ticker minus the downloads) ----------------------
def evaluate(ticker: str, info: dict, hist: pd.DataFrame, income, balance, now: datetime | None = None,
             params: FundamentalParams = FundamentalParams()) -> dict:
    """`ticker` in Yahoo form (e.g. 'TCS.NS'), exactly what the original passed in. `hist` already cleaned of NaN rows."""
    info = info or {}
    t = technicals(hist, params)

    # ---- fundamentals ----
    sector = info.get("sector") or "N/A"
    mcap = num(info.get("marketCap")) / 1e7
    sales_g = num(info.get("revenueGrowth")) * 100
    opm = num(info.get("operatingMargins")) * 100

    net_income = latest(income, ["Net Income", "Net Income Common Stockholders",
                                 "Net Income From Continuing Operation Net Minority Interest"])
    ebit = latest(income, ["EBIT", "Operating Income", "Pretax Income"])
    equity = latest(balance, ["Stockholders Equity", "Total Equity Gross Minority Interest", "Common Stock Equity"])
    assets = latest(balance, ["Total Assets"])
    curr_liab = latest(balance, ["Total Current Liabilities"])

    roe = net_income / equity * 100 if equity else num(info.get("returnOnEquity")) * 100
    cap_employed = assets - curr_liab
    roce = ebit / cap_employed * 100 if cap_employed else 0.0

    eps_curr = num(info.get("trailingEps"))
    eps_hist = annual_values(income, ["Diluted EPS", "Basic EPS"])
    if eps_hist:
        eps_ok = eps_curr > 0 and eps_curr >= eps_hist[0]
    else:
        eps_ok = eps_curr > 0 and num(info.get("earningsGrowth")) > 0

    is_fin = sector == "Financial Services"
    de_ratio = num(info.get("debtToEquity")) / 100          # Yahoo gives it in %
    debt_ok = is_fin or de_ratio <= params.max_debt_equity

    fund_checks = {
        "MCap": mcap > params.min_mcap_cr, "Sales": sales_g > params.min_sales_growth, "OPM": opm > params.min_opm,
        "ROE": roe > params.min_roe, "ROCE": roce > params.min_roce, "EPS": eps_ok, "Debt": debt_ok,
    }
    fund_pass = all(fund_checks.values())

    # ---- valuation ----
    pe = num(info.get("trailingPE")) or num(info.get("forwardPE"))
    peg = num(info.get("pegRatio")) or num(info.get("trailingPegRatio"))
    peg_ok = peg <= 0 or peg <= params.max_peg                # unknown PEG is not penalised
    val_pass = 0 < pe <= params.max_pe and peg_ok

    # ---- price zone + technical confirmation ----
    price_zone = params.dd_min <= t["drawdown"] <= params.dd_max
    tech_pass = t["near_bottom"] and t["higher_low"] and t["momentum_ok"] and t["rr"] >= params.min_rr

    if fund_pass and val_pass and price_zone and tech_pass:
        status = "🔥 PASS"
    elif fund_pass and val_pass and t["drawdown"] >= params.watch_min_dd:
        status = "👀 WATCH"
    elif fund_pass and val_pass:
        status = "WAIT (Price)"
    elif fund_pass:
        status = "WAIT (Valuation)"
    else:
        status = "WAIT (Fund)"

    # ---- human-readable reasons ----
    notes = [f"Fund:{k}" for k, ok in fund_checks.items() if not ok]
    if not val_pass:
        notes.append("Valuation")
    if fund_pass and val_pass:
        if not price_zone:
            notes.append(f"Fall {t['drawdown']:.0f}% (need {params.dd_min}-{params.dd_max}%)")
        if not t["near_bottom"]:
            notes.append("Already bounced far from low")
        if not t["higher_low"]:
            notes.append("No higher-low yet")
        if not t["momentum_ok"]:
            notes.append(f"Momentum {t['tech_score']}/5")
        if t["rr"] < params.min_rr:
            notes.append(f"RR {t['rr']:.1f}")

    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return {
        "Ticker": ticker.split(".")[0], "Market Cap": rnd(mcap), "Current price": rnd(t["cp"]),
        "PE": rnd(pe), "OPM": rnd(opm), "Sales growth": rnd(sales_g),
        "ROE": rnd(roe), "ROCE": rnd(roce), "EPS current": rnd(eps_curr),
        "Volume": int(num(t["volume"])), "RSI": rnd(t["rsi"]), "Status": status, "Sector": sector,
        # new columns
        "Drawdown %": rnd(t["drawdown"]), "Above 52W Low %": rnd(t["bounce"]),
        "Higher Low": "Yes" if t["higher_low"] else "No", "Tech Score": t["tech_score"],
        "Stop Loss": rnd(t["stop"]), "Target 1": rnd(t["t1"]), "Target 2": rnd(t["t2"]),
        "Risk Reward": rnd(t["rr"]), "Notes": "; ".join(notes),
        "Updated": stamp.strftime("%Y-%m-%d %H:%M UTC"),
    }
