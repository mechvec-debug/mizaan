"""
Deep-Value Sentinel - scanner worker

Finds: strong fundamentals + sensible valuation + deep fall from the high (50-70%)
       + bottom forming + daily-chart strength turning up (short/mid-term trade).

Reads tickers from the "Watchlist" tab, writes results to the first tab
of the same Google Sheet and to data.json.
"""
import json
import os
import time
from datetime import datetime, timezone

import gspread
import numpy as np
import pandas as pd
import yfinance as yf
from google.oauth2.service_account import Credentials

# =====================  CONFIGURATION  =====================
SHEET_ID = "1NcJyga0UZh-SYcr5hH0Xbq_-JYX9simpAsOzWh5PtCE"

# 1) Fundamentals (strong business)
MIN_MCAP_CR = 1000      # market cap in crore
MIN_SALES_GROWTH = 8    # %
MIN_OPM = 8             # %
MIN_ROE = 10            # %
MIN_ROCE = 10           # %
MAX_DEBT_EQUITY = 1.0   # ignored for Financial Services

# 2) Valuation
MAX_PE = 35             # trailing PE must be > 0 and <= this
MAX_PEG = 2.0           # only checked when Yahoo provides a PEG

# 3) Price fall from the reference high
HIGH_LOOKBACK_YEARS = 3   # reference high = highest High in last N years
DD_MIN = 35               # PASS zone: price is 50% ...
DD_MAX = 65               # ... to 70% below the reference high
WATCH_MIN_DD = 25         # WATCH if fundamentals+valuation OK and fall >= 40%

# 4) Bottom + 5) Daily upward strength
MAX_BOUNCE_FROM_LOW = 35  # % above 52-week low (still near the bottom)
MIN_TECH_SCORE = 4        # out of 5 momentum checks
MIN_RR = 1.5              # reward:risk to Target 1

REQUEST_PAUSE = 1.2       # seconds between tickers (avoid Yahoo rate limits)
# ===========================================================


# ---------------------- helpers ----------------------
def num(x, default=0.0):
    """Convert anything to a finite float (None / NaN / inf -> default)."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return default if (np.isnan(v) or np.isinf(v)) else v


def rnd(x, n=2):
    return round(num(x), n)


def with_retry(fn, tries=3, wait=3):
    for i in range(tries):
        try:
            return fn()
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(wait * (i + 1))


def get_gspread_client():
    scope = ["https://www.googleapis.com/auth/spreadsheets",
             "https://www.googleapis.com/auth/drive"]
    if os.environ.get("GCP_SERVICE_ACCOUNT"):
        creds_dict = json.loads(os.environ["GCP_SERVICE_ACCOUNT"])
        return gspread.authorize(Credentials.from_service_account_info(creds_dict, scopes=scope))
    return gspread.authorize(Credentials.from_service_account_file("creds.json", scopes=scope))


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


# ---------------------- indicators ----------------------
def rsi_wilder(close, period=14):
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - 100 / (1 + rs)
    rsi = rsi.where(loss != 0, 100.0)
    return rsi.fillna(50.0)


def technicals(hist):
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
    near_bottom = bounce <= MAX_BOUNCE_FROM_LOW

    # Upward strength checks (daily)
    c_ema = cp > ema20.iloc[-1] and ema20.iloc[-1] > ema20.iloc[-6]       # above rising 20 EMA
    c_macd = macd.iloc[-1] > macd_sig.iloc[-1] and macd_hist.iloc[-1] >= macd_hist.iloc[-4]
    c_rsi = 45 <= rsi.iloc[-1] <= 70 and rsi.iloc[-1] > rsi.iloc[-6]       # RSI healthy and rising
    c_vol = vol.iloc[-10:].mean() >= vol.iloc[-50:].mean()                 # volume picking up
    c_ema50 = cp > ema50.iloc[-1]                                          # reclaimed 50 EMA
    checks = [c_ema, c_macd, c_rsi, c_vol, c_ema50]
    tech_score = int(sum(bool(c) for c in checks))
    momentum_ok = bool(c_ema and c_macd and tech_score >= MIN_TECH_SCORE)

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


# ---------------------- per-ticker scan ----------------------
def scan_ticker(ticker):
    stock = yf.Ticker(ticker)
    info = with_retry(lambda: stock.info) or {}
    hist = with_retry(lambda: stock.history(period=f"{HIGH_LOOKBACK_YEARS}y"))
    if hist is None or len(hist) < 120:
        print(f"⏭️  {ticker}: not enough price history, skipped")
        return None
    hist = hist.dropna(subset=["Close", "High", "Low"])

    try:
        income = stock.income_stmt
        balance = stock.balance_sheet
    except Exception:
        income = balance = None

    t = technicals(hist)

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
    debt_ok = is_fin or de_ratio <= MAX_DEBT_EQUITY

    fund_checks = {
        "MCap": mcap > MIN_MCAP_CR, "Sales": sales_g > MIN_SALES_GROWTH, "OPM": opm > MIN_OPM,
        "ROE": roe > MIN_ROE, "ROCE": roce > MIN_ROCE, "EPS": eps_ok, "Debt": debt_ok,
    }
    fund_pass = all(fund_checks.values())

    # ---- valuation ----
    pe = num(info.get("trailingPE")) or num(info.get("forwardPE"))
    peg = num(info.get("pegRatio")) or num(info.get("trailingPegRatio"))
    peg_ok = peg <= 0 or peg <= MAX_PEG                      # unknown PEG is not penalised
    val_pass = 0 < pe <= MAX_PE and peg_ok

    # ---- price zone + technical confirmation ----
    price_zone = DD_MIN <= t["drawdown"] <= DD_MAX
    tech_pass = t["near_bottom"] and t["higher_low"] and t["momentum_ok"] and t["rr"] >= MIN_RR

    if fund_pass and val_pass and price_zone and tech_pass:
        status = "🔥 PASS"
    elif fund_pass and val_pass and t["drawdown"] >= WATCH_MIN_DD:
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
            notes.append(f"Fall {t['drawdown']:.0f}% (need {DD_MIN}-{DD_MAX}%)")
        if not t["near_bottom"]:
            notes.append("Already bounced far from low")
        if not t["higher_low"]:
            notes.append("No higher-low yet")
        if not t["momentum_ok"]:
            notes.append(f"Momentum {t['tech_score']}/5")
        if t["rr"] < MIN_RR:
            notes.append(f"RR {t['rr']:.1f}")

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
        "Updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }


# ---------------------- main ----------------------
STATUS_ORDER = {"🔥 PASS": 0, "👀 WATCH": 1, "WAIT (Price)": 2, "WAIT (Valuation)": 3, "WAIT (Fund)": 4}


def analyze_stocks():
    print("🚦 Starting Deep-Value scan...")
    gc = get_gspread_client()
    spreadsheet = gc.open_by_key(SHEET_ID)
    raw = spreadsheet.worksheet("Watchlist").col_values(1)[1:]

    tickers, seen = [], set()
    for t in raw:
        t = t.strip().upper()
        if not t or t in seen:
            continue
        seen.add(t)
        tickers.append(t if "." in t else t + ".NS")

    results, failed = [], []
    for ticker in tickers:
        try:
            row = scan_ticker(ticker)
            if row:
                results.append(row)
                print(f"✅ {row['Ticker']:<12} {row['Status']:<17} fall {row['Drawdown %']}% tech {row['Tech Score']}/5")
        except Exception as e:  # one bad ticker must never break the run
            failed.append(ticker)
            print(f"⚠️ {ticker} failed: {type(e).__name__}: {e}")
        time.sleep(REQUEST_PAUSE)

    if not results:
        # Keep the old sheet data; exit cleanly but show a warning in the Actions UI
        print("::warning::No stock produced data this run (Yahoo rate limit or empty watchlist). Sheet left unchanged.")
        return

    df = pd.DataFrame(results)
    df["_o"] = df["Status"].map(STATUS_ORDER).fillna(9)
    df = df.sort_values(["_o", "Tech Score"], ascending=[True, False]).drop(columns="_o")
    df = df.replace([np.inf, -np.inf], 0).fillna(0)

    data = json.loads(df.to_json(orient="values", force_ascii=False))   # JSON-safe python types
    values = [df.columns.tolist()] + data

    ws = spreadsheet.sheet1
    ws.resize(rows=max(len(values) + 20, 100), cols=max(len(values[0]), 26))
    ws.clear()
    ws.update(range_name="A1", values=values, value_input_option="USER_ENTERED")
    print("✅ Google Sheet refreshed.")

    df.to_json("data.json", orient="records", indent=4, force_ascii=False)
    print("💾 data.json written.")

    counts = df["Status"].value_counts().to_dict()
    print(f"📊 Summary: {counts} | failed tickers: {len(failed)}")


if __name__ == "__main__":
    analyze_stocks()
