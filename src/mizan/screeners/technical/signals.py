"""Turns an EngineResult into the dashboard record: state text, aligned/plan wording, levels, grade (decisions D1-D9).
Wording is the production wording found in the stored alerts (see docs/TECHNICAL_STATES.md)."""
from __future__ import annotations

import math
from dataclasses import dataclass

from .config import TechnicalConfig
from .engine import EngineResult, HtfView

# ---- state texts (FORMING / VOID are matched by the original dashboard's hidden-row regex)
S_TRIGGERED = "🟢 BUY TRIGGERED"
S_IN_ZONE = "🔵 IN BUY ZONE"
S_WAIT = "🟡 WAIT FOR BUY ZONE"
S_FORMING = "⚪ FORMING"
S_VOID = "✖ VOID"
S_TARGET = "🏁 TARGET HIT"
ACTIONABLE_STATES = (S_TRIGGERED, S_IN_ZONE)

PLAN_BUY = "BUY pullbacks"
PLAN_LOWCONV = "low conviction — wait"
PLAN_AVOID = "AVOID: Macro Downtrend"
PLAN_CAUTION = "CAUTION: Low Conviction"

STATE_RANK = {S_TRIGGERED: 0, S_IN_ZONE: 1, S_WAIT: 2}


@dataclass
class MtfSummary:
    bias_dir: int
    bull: int
    bear: int
    n: int
    conviction: float
    low_conv: bool
    aligned: str
    plan: str


def mtf_summary(views: list[HtfView], cfg: TechnicalConfig) -> MtfSummary:
    n = len(views)
    bull = sum(1 for v in views if v.bias > 0)
    bear = sum(1 for v in views if v.bias < 0)
    bias_dir = 1 if bull > bear else (-1 if bear > bull else 0)
    voters = [v.strength for v in views if (v.bias > 0 if bias_dir > 0 else v.bias < 0)] if bias_dir != 0 else []
    conviction = sum(voters) / len(voters) if voters else 0.0
    low_conv = bias_dir != 0 and cfg.plan_str_floor > 0 and conviction < cfg.plan_str_floor
    if n == 0:
        aligned = "NO HIGHER TF"
    elif bull == n:
        aligned = "ALIGNED ▲ LONGS"
    elif bear == n:
        aligned = "ALIGNED ▼ SHORTS"
    elif bull > bear:
        aligned = "▲ BULLISH BIAS"
    elif bear > bull:
        aligned = "▼ BEARISH BIAS"
    else:
        aligned = "MIXED — WEAK EDGE"
    if bias_dir > 0:
        plan = PLAN_LOWCONV if low_conv else PLAN_BUY
    elif bias_dir < 0:
        plan = PLAN_AVOID                       # D2: long-only strategy
    else:
        plan = PLAN_CAUTION
    return MtfSummary(bias_dir, bull, bear, n, conviction, low_conv, aligned, plan)


def state_text(r: EngineResult) -> str:
    if r.zone_side == 0:
        return S_FORMING                        # D1: no qualified upward impulse yet
    if r.z_state == 3:
        return S_VOID
    if r.z_state == 2:
        return S_TARGET
    if r.valid_buy:
        return S_TRIGGERED
    return S_IN_ZONE if r.z_state == 1 else S_WAIT


def levels(r: EngineResult, cfg: TechnicalConfig) -> dict:
    """Entry = latest close; risk = ATR x multiplier; TPn = entry + n x risk (same on every row, as in the stored alerts)."""
    risk = r.atr_risk * cfg.sl_atr_mult
    e = r.close
    return {"entry": round(e, 2), "sl": round(e - risk, 2), "tp1": round(e + risk * cfg.rr1, 2),
            "tp2": round(e + risk * cfg.rr2, 2), "tp3": round(e + risk * cfg.rr3, 2)}


def rr_at_close(r: EngineResult) -> float | None:
    """(chart TP1 - close) / (close - chart stop): reward/risk of taking the setup at today's close."""
    if r.zone_side != 1 or math.isnan(r.chart_stop) or math.isnan(r.chart_tp1):
        return None
    risk = r.close - r.chart_stop
    return round((r.chart_tp1 - r.close) / risk, 2) if risk > 0 else None


def grade(state: str, plan: str, rr: float | None, stale: bool, cfg: TechnicalConfig) -> tuple[str, str]:
    """A / B / C / '' plus a one-line reason (D6, D7). Thresholds live in config/indicators.yaml."""
    if stale:
        return "", "stale price data - not graded"
    rr_txt = f"R:R {rr:.2f}" if rr is not None else "R:R n/a"
    if state in ACTIONABLE_STATES:
        where = "buy signal inside the zone" if state == S_TRIGGERED else "inside the 0.382–0.618 retracement zone"
        if plan == PLAN_BUY:
            if rr is not None and rr >= cfg.min_rr_for_a:
                return "A", f"weekly+monthly uptrend, {where}, {rr_txt}"
            return "B", f"uptrend, {where}, but {rr_txt} < {cfg.min_rr_for_a}"
        if plan in (PLAN_LOWCONV, PLAN_CAUTION):
            why = "higher-timeframe trend is weak" if plan == PLAN_LOWCONV else "weekly and monthly disagree"
            return "B", f"{where}, but {why}, {rr_txt}"
        return "", "macro downtrend - avoid"
    if state == S_WAIT:
        if plan == PLAN_AVOID:
            return "", "macro downtrend - avoid"
        return "C", "uptrend impulse found; price has not pulled back into the zone yet"
    return "", {S_FORMING: "no qualified upward impulse yet", S_VOID: "setup stopped out", S_TARGET: "first target already reached"}.get(state, "")


def sort_key(rec: dict):
    d = rec["details"]
    g = {"A": 0, "B": 1, "C": 2, "": 3}[d["grade"]]
    return (g, STATE_RANK.get(rec["action"], 9), -(d["rr_at_close"] if d["rr_at_close"] is not None else -9), d["setup_age_bars"] if d["setup_age_bars"] is not None else 9999)
