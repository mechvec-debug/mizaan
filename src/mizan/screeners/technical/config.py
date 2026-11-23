"""Technical screener configuration (config/indicators.yaml). Core indicators define the strategy: their parameters can be
edited, but they cannot be disabled. Optional indicators (rsi, macd, volume_ratio) are shown as extra columns when enabled."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ...core.errors import ConfigError
from ...core.paths import project_root

CORE = {"ema_fast", "ema_slow", "atr_risk", "rr_targets", "swing_engine", "adx"}
OPTIONAL = {"rsi", "macd", "volume_ratio"}


@dataclass(frozen=True)
class TechnicalConfig:
    # --- Pine "Trade Execution" inputs
    ema_fast: int = 9
    ema_slow: int = 21
    sl_atr_len: int = 14
    sl_atr_mult: float = 2.0
    rr1: float = 1.0
    rr2: float = 2.0
    rr3: float = 3.0
    # --- Pine "Adaptive Impulse Engine" inputs
    atr_len: int = 14
    swing_atr_mult: float = 0.7
    pivot_left: int = 8
    pivot_right: int = 3
    impulse_min: float = 55.0
    max_pivots: int = 30
    # --- Pine constants (HUD)
    adx_len: int = 14
    adx_floor: float = 10.0
    adx_ceil: float = 50.0
    plan_str_floor: float = 20.0
    fib_zone_hi: float = 0.382
    fib_zone_lo: float = 0.618
    fib_ext_1: float = 0.272
    stop_atr_pad: float = 0.2
    # --- scanner decisions (D3, D5, D7, D6)
    cross_lookback_bars: int = 3
    min_bars: int = 250
    stale_bar_days: int = 7
    min_rr_for_a: float = 1.2
    timezone: str = "Asia/Kolkata"
    extras: tuple = field(default_factory=tuple)        # ((name, params-dict-as-tuple), ...)

    def validate(self) -> "TechnicalConfig":
        bad = []
        for name in ("ema_fast", "ema_slow", "sl_atr_len", "atr_len", "pivot_left", "pivot_right", "adx_len", "max_pivots", "cross_lookback_bars"):
            if getattr(self, name) < 1:
                bad.append(f"{name} must be >= 1")
        if self.ema_fast >= self.ema_slow:
            bad.append("ema_fast must be shorter than ema_slow")
        if not (self.rr1 > 0 and self.rr2 >= self.rr1 and self.rr3 >= self.rr2):
            bad.append("rr1 <= rr2 <= rr3 and all > 0 required")
        if self.min_bars < self.pivot_left + self.pivot_right + 20:
            bad.append("min_bars is too small for the pivot window")
        if bad:
            raise ConfigError("Invalid technical configuration: " + "; ".join(bad))
        return self


def load_technical_config(path: str | Path | None = None) -> TechnicalConfig:
    p = Path(path) if path else project_root() / "config" / "indicators.yaml"
    try:
        with open(p, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
    except FileNotFoundError as exc:
        raise ConfigError(f"Config file not found: {p}") from exc
    items = {i["name"]: i for i in raw.get("indicators", [])}
    for name in CORE:
        if name in items and not items[name].get("enabled", True):
            raise ConfigError(f"Core indicator '{name}' defines the strategy and cannot be disabled (edit its parameters instead).")
    unknown = set(items) - CORE - OPTIONAL
    if unknown:
        raise ConfigError(f"Unknown indicator(s) in {p.name}: {sorted(unknown)}")
    g = lambda n: dict(items.get(n, {}).get("params", {}))
    kw: dict = {}
    m = {"ema_fast": ("ema_fast", "length"), "ema_slow": ("ema_slow", "length")}
    for key, (ind, par) in m.items():
        if par in g(ind):
            kw[key] = int(g(ind)[par])
    r = g("atr_risk"); kw.update({k: v for k, v in (("sl_atr_len", r.get("length")), ("sl_atr_mult", r.get("multiplier"))) if v is not None})
    t = g("rr_targets"); kw.update({k: float(t[v]) for k, v in (("rr1", "tp1"), ("rr2", "tp2"), ("rr3", "tp3")) if v in t})
    s = g("swing_engine")
    for k, v in (("atr_len", "atr_length"), ("swing_atr_mult", "min_move_atr"), ("pivot_left", "pivot_left"), ("pivot_right", "pivot_right"), ("impulse_min", "impulse_min_score")):
        if v in s:
            kw[k] = s[v]
    if "length" in g("adx"):
        kw["adx_len"] = int(g("adx")["length"])
    sig, grade, out = raw.get("signal", {}), raw.get("grade", {}), raw.get("output", {})
    for k in ("cross_lookback_bars", "min_bars", "stale_bar_days"):
        if k in sig:
            kw[k] = int(sig[k])
    if "min_rr_for_a" in grade:
        kw["min_rr_for_a"] = float(grade["min_rr_for_a"])
    if "min_conviction" in grade:
        kw["plan_str_floor"] = float(grade["min_conviction"])
    if "timezone" in out:
        kw["timezone"] = out["timezone"]
    kw["extras"] = tuple((n, tuple(sorted(i.get("params", {}).items()))) for n, i in items.items() if n in OPTIONAL and i.get("enabled", False))
    return TechnicalConfig(**kw).validate()
