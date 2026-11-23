"""Bar-by-bar Python port of the Pine 'Impulse Waves & Reversal Zones' engine (long-only) + EMA/ATR trade layer.

Faithful to the script's evaluation order:
  1. every bar: update the pivot array (ta.pivothigh/low confirmed `right` bars late)
  2. draw block on every NEW PIVOT bar and on the LAST bar: CHoCH trend -> leg scores -> impulse -> zone/stop/target/state
  3. every bar after that: per-bar terminal latch (stopped / target) against the persisted levels
Higher-timeframe bias uses the last COMPLETED weekly / monthly bar (Pine's t[1], adx[1]).
Nothing here looks at the future: the result describes the LAST bar of the frame passed in.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import indicators as ix
from .config import TechnicalConfig

EPS = 1e-9
NAN = math.nan


@dataclass
class HtfView:
    bias: int = 0
    strength: float = 0.0
    bars: int = 0


@dataclass
class EngineResult:
    bars: int
    last_date: str
    close: float
    atr_risk: float
    atr_swing: float
    ema_fast: float
    ema_slow: float
    crossed_recent: bool
    zone_side: int                  # 1 = a qualified upward impulse (long setup) exists
    z_state: int                    # 0 wait | 1 in zone | 2 target hit | 3 stopped   (only meaningful if zone_side == 1)
    z_hi: float
    z_lo: float
    chart_tp1: float
    chart_stop: float
    rr_mid: float                   # Pine's rrVal (entry at zone midpoint)
    impulse_start: float
    impulse_end: float
    impulse_end_bar: int
    price_in_zone: bool
    valid_buy: bool
    chart_trend: int
    pivots: int
    weekly: HtfView = field(default_factory=HtfView)
    monthly: HtfView = field(default_factory=HtfView)


def _choch(pv_price, pv_high):
    n = len(pv_price)
    trend_at, trend = [0] * n, 0
    lh = ll = pH = pL = None
    for i in range(n):
        pr = pv_price[i]
        if pv_high[i]:
            if trend == 1:
                if lh is None or pr > lh:
                    pL = ll
            else:
                ref = pH if trend < 0 else lh
                if ref is not None and pr > ref:
                    trend, pL = 1, ll
            lh = pr
        else:
            if trend == -1:
                if ll is None or pr < ll:
                    pH = lh
            else:
                ref2 = pL if trend > 0 else ll
                if ref2 is not None and pr < ref2:
                    trend, pH = -1, lh
            ll = pr
        trend_at[i] = trend
    return trend_at, trend


def _trade_impulse(pv_price, pv_high, pv_cum, pv_atr, impulse_min):
    """Returns (index of last qualified UPWARD impulse leg or -1, final CHoCH trend)."""
    n = len(pv_price)
    trend_at, trend = _choch(pv_price, pv_high)
    leg_n = n - 1
    disp = np.empty(leg_n)
    eff = np.empty(leg_n)
    for i in range(leg_n):
        sz = abs(pv_price[i + 1] - pv_price[i])
        a1, a2 = pv_atr[i], pv_atr[i + 1]
        avg = (a1 + a2) / 2.0
        aref = NAN if math.isnan(avg) else max(avg, EPS)
        disp[i] = sz / aref if not math.isnan(aref) else NAN
        path = pv_cum[i + 1] - pv_cum[i]
        eff[i] = min(sz / path, 1.0) if path > EPS else 1.0
    with np.errstate(invalid="ignore"):
        below = (disp[None, :] < disp[:, None]).sum(axis=1)       # NaN compares False, as in Pine
    pct = below / float(leg_n - 1) if leg_n > 1 else np.full(leg_n, 0.5)
    score = np.clip(100.0 * (0.5 * eff + 0.5 * pct), 0.0, 100.0)
    idx = -1
    for i in range(leg_n):
        up = pv_price[i + 1] > pv_price[i]
        trv = trend_at[i + 1]
        counter = (trv == 1 and not up) or (trv == -1 and up)
        if not counter and score[i] >= impulse_min and up:
            idx = i
    return idx, trend


def run_engine(df: pd.DataFrame, cfg: TechnicalConfig) -> EngineResult:
    o = df["Open"].to_numpy(float)  # noqa: F841  (kept for symmetry / debugging)
    h, l, c = (df[k].to_numpy(float) for k in ("High", "Low", "Close"))
    n = len(c)
    atr_sw = ix.atr(h, l, c, cfg.atr_len)
    atr_rk = ix.atr(h, l, c, cfg.sl_atr_len)
    ph, pl = ix.pivot_high(h, cfg.pivot_left, cfg.pivot_right), ix.pivot_low(l, cfg.pivot_left, cfg.pivot_right)
    path_cum = np.cumsum(np.r_[0.0, np.abs(np.diff(c))])
    ef, es = ix.ema(c, cfg.ema_fast), ix.ema(c, cfg.ema_slow)
    cross = ix.crossover(ef, es)

    pv_price: list[float] = []
    pv_bar: list[int] = []
    pv_high: list[bool] = []
    pv_cum: list[float] = []
    pv_atr: list[float] = []

    zone_side, z_state = 0, 0
    z_hi = z_lo = z_t1 = z_sl = rr_val = NAN
    imp_s = imp_e = NAN
    imp_bar = -1
    anchor, latch = -1, 0
    chart_trend = 0

    def push(p, is_high, i):
        pv_price.append(p); pv_bar.append(i - cfg.pivot_right); pv_high.append(is_high)
        pv_cum.append(path_cum[i]); pv_atr.append(atr_sw[i])

    for i in range(n):
        a_i = atr_sw[i]
        hv, lv = ph[i], pl[i]
        new_pivot = not math.isnan(hv) or not math.isnan(lv)
        if not math.isnan(hv):
            if not pv_price or not pv_high[-1]:
                if not pv_price or (not math.isnan(a_i) and abs(hv - pv_price[-1]) >= a_i * cfg.swing_atr_mult):
                    push(hv, True, i)
            elif hv > pv_price[-1]:
                pv_price[-1], pv_bar[-1], pv_cum[-1] = hv, i - cfg.pivot_right, path_cum[i]
        if not math.isnan(lv):
            if not pv_price or pv_high[-1]:
                if not pv_price or (not math.isnan(a_i) and abs(lv - pv_price[-1]) >= a_i * cfg.swing_atr_mult):
                    push(lv, False, i)
            elif lv < pv_price[-1]:
                pv_price[-1], pv_bar[-1], pv_cum[-1] = lv, i - cfg.pivot_right, path_cum[i]
        while len(pv_price) > cfg.max_pivots:
            for lst in (pv_price, pv_bar, pv_high, pv_cum, pv_atr):
                lst.pop(0)

        if (new_pivot or i == n - 1) and len(pv_price) >= 3:                 # ---- draw block
            zone_side, z_state = 0, 0
            idx, chart_trend = _trade_impulse(pv_price, pv_high, pv_cum, pv_atr, cfg.impulse_min)
            if idx >= 0:
                ip1, ip2, ib2 = pv_price[idx], pv_price[idx + 1], pv_bar[idx + 1]
                rng = abs(ip2 - ip1)
                z382, z500, z618 = ip2 - rng * cfg.fib_zone_hi, ip2 - rng * 0.5, ip2 - rng * cfg.fib_zone_lo
                t1 = ip2 + rng * cfg.fib_ext_1
                sl = ip1 - a_i * cfg.stop_atr_pad
                zone_side = 1
                z_hi, z_lo, z_t1, z_sl = max(z382, z618), min(z382, z618), t1, sl
                imp_s, imp_e, imp_bar = ip1, ip2, ib2
                risk = abs(z500 - sl)
                rr_val = abs(z500 - t1) / risk if risk > EPS else NAN
                if anchor != ib2:
                    anchor, latch = ib2, 0
                hit_sl, hit_tp = l[i] <= sl, h[i] >= t1
                if latch == 0:
                    latch = 3 if hit_sl else (2 if hit_tp else 0)
                in_zn = z_lo <= c[i] <= z_hi
                z_state = latch if latch != 0 else (1 if in_zn else 0)

        if zone_side != 0 and not math.isnan(z_sl) and not math.isnan(z_t1):    # ---- per-bar latch
            if latch == 0:
                b_sl, b_tp = l[i] <= z_sl, h[i] >= z_t1
                latch = 3 if b_sl else (2 if b_tp else 0)
        else:
            latch = 0

    price_in_zone = zone_side != 0 and not math.isnan(z_hi) and z_lo <= c[-1] <= z_hi
    lookback = max(1, cfg.cross_lookback_bars)
    crossed_recent = bool(cross[-lookback:].any())
    valid_buy = bool(crossed_recent and zone_side == 1 and price_in_zone)

    def htf(kind):
        bars = ix.resample_htf(df, kind)
        if len(bars) < 2:
            return HtfView(0, 0.0, len(bars))
        trend, adx = ix.htf_state_series(bars, cfg.pivot_left, cfg.pivot_right, cfg.adx_len)
        k = len(bars) - 2                       # last COMPLETED higher-timeframe bar (Pine: t[1], adx[1])
        return HtfView(int(trend[k]), ix.adx_strength(adx[k], cfg.adx_floor, cfg.adx_ceil), len(bars))

    return EngineResult(
        bars=n, last_date=str(df.index[-1].date()), close=float(c[-1]), atr_risk=float(atr_rk[-1]), atr_swing=float(atr_sw[-1]),
        ema_fast=float(ef[-1]), ema_slow=float(es[-1]), crossed_recent=crossed_recent, zone_side=zone_side, z_state=z_state,
        z_hi=float(z_hi), z_lo=float(z_lo), chart_tp1=float(z_t1), chart_stop=float(z_sl), rr_mid=float(rr_val),
        impulse_start=float(imp_s), impulse_end=float(imp_e), impulse_end_bar=int(imp_bar), price_in_zone=bool(price_in_zone),
        valid_buy=valid_buy, chart_trend=int(chart_trend), pivots=len(pv_price), weekly=htf("weekly"), monthly=htf("monthly"))
