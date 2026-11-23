"""Synthetic price paths for the technical-engine tests (piecewise-linear closes with a thin high/low envelope)."""
import numpy as np
import pandas as pd


def path(segments, start=100.0):
    """segments: [(bars, target_price), ...] -> close array (linear moves)."""
    out, cur = [], start
    for bars, tgt in segments:
        out += list(np.linspace(cur, tgt, bars + 1)[1:])
        cur = tgt
    return np.array(out)


def frame(close, end="2026-10-30"):
    close = np.asarray(close, float)
    idx = pd.bdate_range(end=end, periods=len(close))
    op = np.r_[close[0], close[:-1]]
    hi = np.maximum(op, close) * 1.004
    lo = np.minimum(op, close) * 0.996
    return pd.DataFrame({"Open": op, "High": hi, "Low": lo, "Close": close, "Volume": 100000.0}, index=idx)


# a warm-up so ATR/pivots exist, then a zig-zag uptrend whose LAST up-leg is the largest impulse (114 -> 140)
BASE = [(60, 100), (12, 101), (12, 99.5)]
UPTREND = [(12, 112), (10, 105), (14, 122), (10, 114), (16, 140)]
ZONE = (123.9, 130.1)          # 0.618 / 0.382 retracement of 114->140 (approx; engine decides exactly)
