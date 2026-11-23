"""Pine-exact building blocks (ta.rma, ta.atr, ta.ema, ta.pivothigh/low, the script's own ADX) plus optional extras.
All functions are pure (numpy in, numpy out) so each can be unit-tested against hand-checked values."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd


def rma(x: np.ndarray, n: int) -> np.ndarray:
    """Pine ta.rma: Wilder smoothing. NaN until n consecutive valid values exist (seed = their mean), then
    rma = (prev*(n-1) + x)/n. A NaN input restarts the seeding, exactly like Pine."""
    x = np.asarray(x, dtype=float)
    out = np.full(len(x), np.nan)
    run, prev = 0, math.nan
    for i, v in enumerate(x):
        if math.isnan(v):
            run, prev = 0, math.nan
            continue
        run += 1
        if math.isnan(prev):
            if run >= n:
                prev = float(np.mean(x[i - n + 1:i + 1]))
                out[i] = prev
        else:
            prev = (prev * (n - 1) + v) / n
            out[i] = prev
    return out


def ema(x: np.ndarray, n: int) -> np.ndarray:
    """Pine ta.ema (alpha = 2/(n+1), seeded with the first value)."""
    return pd.Series(np.asarray(x, dtype=float)).ewm(alpha=2.0 / (n + 1), adjust=False).mean().to_numpy()


def true_range(h: np.ndarray, l: np.ndarray, c: np.ndarray) -> np.ndarray:
    pc = np.r_[np.nan, c[:-1]]
    tr = np.maximum.reduce([h - l, np.abs(h - pc), np.abs(l - pc)])
    tr[0] = h[0] - l[0]
    return tr


def atr(h, l, c, n: int) -> np.ndarray:
    return rma(true_range(h, l, c), n)


def adx_value(h: np.ndarray, l: np.ndarray, c: np.ndarray, n: int) -> np.ndarray:
    """The script's own adxVal(): DI/ADX with diLength = adxSmoothing = n (same maths as ta.dmi)."""
    up = np.r_[np.nan, np.diff(h)]
    down = -np.r_[np.nan, np.diff(l)]
    with np.errstate(invalid="ignore"):
        plus_dm = np.where(np.isnan(up), 0.0, np.where((up > down) & (up > 0), up, 0.0))
        minus_dm = np.where(np.isnan(down), 0.0, np.where((down > up) & (down > 0), down, 0.0))
    trur = rma(true_range(h, l, c), n)
    with np.errstate(invalid="ignore", divide="ignore"):
        plus = np.where(trur == 0, 0.0, 100.0 * rma(plus_dm, n) / trur)
        minus = np.where(trur == 0, 0.0, 100.0 * rma(minus_dm, n) / trur)
        s = plus + minus
        dx = np.abs(plus - minus) / np.where(s == 0.0, 1.0, s)
    return 100.0 * rma(dx, n)


def adx_strength(a: float, floor: float = 10.0, ceil: float = 50.0) -> float:
    """Pine adxStr(): 0 at/below floor, 100 at/above ceil, NaN -> 0."""
    if a is None or math.isnan(a):
        return 0.0
    return float(min(100.0, max(0.0, (a - floor) / (ceil - floor) * 100.0)))


def pivot_high(h: np.ndarray, left: int, right: int) -> np.ndarray:
    """Pine ta.pivothigh: value reported on the CONFIRMATION bar (right bars after the pivot).
    Tie rule (documented, to be confirmed by the TradingView spot-check): strictly higher than the `left` bars before,
    not lower than the `right` bars after -> the FIRST of equal highs is the pivot."""
    n = len(h)
    out = np.full(n, np.nan)
    for i in range(left + right, n):
        j = i - right
        v = h[j]
        if v > h[j - left:j].max() and v >= h[j + 1:i + 1].max():
            out[i] = v
    return out


def pivot_low(l: np.ndarray, left: int, right: int) -> np.ndarray:
    n = len(l)
    out = np.full(n, np.nan)
    for i in range(left + right, n):
        j = i - right
        v = l[j]
        if v < l[j - left:j].min() and v <= l[j + 1:i + 1].min():
            out[i] = v
    return out


def crossover(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Pine ta.crossover: a > b now and a <= b on the previous bar."""
    out = np.zeros(len(a), dtype=bool)
    out[1:] = (a[1:] > b[1:]) & (a[:-1] <= b[:-1])
    return out


# ---------------------------------------------------------------- higher timeframes
def resample_htf(df: pd.DataFrame, kind: str) -> pd.DataFrame:
    """Daily -> weekly (Mon-Fri, labelled by Friday) or monthly bars, as TradingView builds them from daily data."""
    rule = {"weekly": "W-FRI", "monthly": "ME"}[kind]
    agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
    try:
        out = df.resample(rule).agg(agg)
    except ValueError:                       # older pandas spells month-end 'M'
        out = df.resample("M").agg(agg)
    return out.dropna(subset=["Close"])


def htf_state_series(df: pd.DataFrame, left: int, right: int, adx_len: int) -> tuple[np.ndarray, np.ndarray]:
    """The script's htfState(): incremental CHoCH state machine on higher-timeframe bars. Returns (trend_after_bar, adx_after_bar).
    The caller uses index [k-1] for the bar k that contains the latest daily bar (Pine returns t[1] / adx[1])."""
    h, l, c = df["High"].to_numpy(float), df["Low"].to_numpy(float), df["Close"].to_numpy(float)
    ph, pl = pivot_high(h, left, right), pivot_low(l, left, right)
    n = len(h)
    t, lh, ll, pH, pL = 0, None, None, None, None
    trend = np.zeros(n, dtype=int)
    for i in range(n):
        hv, lv = ph[i], pl[i]
        if not math.isnan(hv):
            if t == 1:
                if lh is None or hv > lh:
                    pL = ll
            else:
                ref = pH if t < 0 else lh
                if ref is not None and hv > ref:
                    t, pL = 1, ll
            lh = hv
        if not math.isnan(lv):
            if t == -1:
                if ll is None or lv < ll:
                    pH = lh
            else:
                ref2 = pL if t > 0 else ll
                if ref2 is not None and lv < ref2:
                    t, pH = -1, lh
            ll = lv
        trend[i] = t
    return trend, adx_value(h, l, c, adx_len)


# ---------------------------------------------------------------- optional extras (shown as columns when enabled)
def extras_latest(df: pd.DataFrame, enabled: tuple) -> dict:
    out = {}
    c, v = df["Close"].to_numpy(float), df["Volume"].to_numpy(float)
    for name, params in enabled:
        p = dict(params)
        if name == "rsi":
            n = int(p.get("length", 14))
            d = np.r_[np.nan, np.diff(c)]
            up, dn = rma(np.where(np.isnan(d), np.nan, np.maximum(d, 0)), n), rma(np.where(np.isnan(d), np.nan, np.maximum(-d, 0)), n)
            with np.errstate(divide="ignore", invalid="ignore"):
                rs = np.where(dn == 0, np.inf, up / dn)      # no losses -> RSI 100
            out["rsi"] = round(float(100 - 100 / (1 + rs[-1])), 2) if not math.isnan(up[-1]) else None
        elif name == "macd":
            f, s, g = int(p.get("fast", 12)), int(p.get("slow", 26)), int(p.get("signal", 9))
            line = ema(c, f) - ema(c, s)
            sig = ema(line, g)
            out["macd"], out["macd_signal"] = round(float(line[-1]), 4), round(float(sig[-1]), 4)
        elif name == "volume_ratio":
            a, b = int(p.get("short", 10)), int(p.get("long", 50))
            out["volume_ratio"] = round(float(v[-a:].mean() / v[-b:].mean()), 3) if len(v) >= b and v[-b:].mean() > 0 else None
    return out
