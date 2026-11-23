"""Phase 5 tests - Swing Setup Radar: indicators, engine (state machine), signals. No network, no randomness in assertions."""
import dataclasses
import math
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from fakes import make_history  # noqa: E402
from tech_paths import BASE, UPTREND, frame, path  # noqa: E402
from mizan.core.errors import ConfigError  # noqa: E402
from mizan.screeners.technical import indicators as ix  # noqa: E402
from mizan.screeners.technical import signals as sg  # noqa: E402
from mizan.screeners.technical.config import TechnicalConfig, load_technical_config  # noqa: E402
from mizan.screeners.technical.engine import EngineResult, HtfView, run_engine  # noqa: E402

CFG = load_technical_config()


class TestIndicators(unittest.TestCase):
    def test_rma_seed_and_recursion(self):
        r = ix.rma(np.array([1, 2, 3, 4, 5, 6.0]), 3)
        self.assertTrue(np.isnan(r[:2]).all())
        np.testing.assert_allclose(r[2:], [2.0, 2 + 2 / 3, (8 / 3 * 2 + 5) / 3, ((8 / 3 * 2 + 5) / 3 * 2 + 6) / 3])

    def test_rma_restarts_after_nan(self):
        r = ix.rma(np.array([1, 2, 3, np.nan, 10, 10, 10, 10.0]), 3)
        self.assertTrue(np.isnan(r[3]) and np.isnan(r[4]) and np.isnan(r[5]))
        self.assertEqual(r[6], 10.0)

    def test_rma_of_constant_is_constant(self):
        np.testing.assert_allclose(ix.rma(np.full(40, 7.5), 14)[13:], 7.5)

    def test_true_range_and_atr(self):
        h, l, c = np.array([10, 12, 11.0]), np.array([9, 10, 8.0]), np.array([9.5, 11, 10.0])
        np.testing.assert_allclose(ix.true_range(h, l, c), [1.0, 2.5, 3.0])     # bar0 = H-L; bar1 = max(2, |12-9.5|, |10-9.5|)
        a = ix.atr(np.full(30, 11.0), np.full(30, 9.0), np.full(30, 10.0), 14)
        np.testing.assert_allclose(a[13:], 2.0)

    def test_ema_matches_definition(self):
        x = np.array([1, 2, 4, 8.0])
        e = ix.ema(x, 3)                                   # alpha = 0.5
        np.testing.assert_allclose(e, [1, 1.5, 2.75, 5.375])

    def test_pivots_confirmation_and_tie_rule(self):
        h = np.array([1, 2, 3, 9, 3, 2, 1, 1, 1, 1.0])
        ph = ix.pivot_high(h, 3, 3)
        self.assertEqual(np.where(~np.isnan(ph))[0].tolist(), [6])        # pivot at bar 3, reported on confirmation bar 6
        self.assertEqual(ph[6], 9.0)
        tie = np.array([1, 2, 3, 9, 9, 2, 1, 1, 1, 1.0])
        first = np.where(~np.isnan(ix.pivot_high(tie, 3, 3)))[0]
        self.assertEqual(first.tolist(), [6])                              # FIRST of two equal highs wins (bar 3 -> reported at 6)
        l = np.array([9, 8, 7, 1, 7, 8, 9, 9, 9, 9.0])
        self.assertEqual(np.where(~np.isnan(ix.pivot_low(l, 3, 3)))[0].tolist(), [6])

    def test_crossover_definition(self):
        a, b = np.array([1, 2, 3, 3.0]), np.array([2, 2, 2, 4.0])
        self.assertEqual(ix.crossover(a, b).tolist(), [False, False, True, False])

    def test_adx_strength_scale(self):
        self.assertEqual((ix.adx_strength(5), ix.adx_strength(10), ix.adx_strength(30), ix.adx_strength(50), ix.adx_strength(80)), (0.0, 0.0, 50.0, 100.0, 100.0))
        self.assertEqual(ix.adx_strength(float("nan")), 0.0)

    def test_adx_high_in_trend_low_in_chop(self):
        up = frame(path([(120, 200)], 100))
        rng = np.random.default_rng(0)
        chop = frame(100 + np.cumsum(rng.normal(0, 0.2, 120)) * 0 + np.sin(np.arange(120)) * 1.5)
        a_up = ix.adx_value(*(up[k].to_numpy() for k in ("High", "Low", "Close")), 14)[-1]
        a_chop = ix.adx_value(*(chop[k].to_numpy() for k in ("High", "Low", "Close")), 14)[-1]
        self.assertGreater(a_up, 50)
        self.assertLess(a_chop, 25)

    def test_adx_first_valid_bar(self):
        f = frame(path([(80, 150)], 100))
        a = ix.adx_value(*(f[k].to_numpy() for k in ("High", "Low", "Close")), 14)
        self.assertTrue(np.isnan(a[:26]).all() and not np.isnan(a[26]))     # needs 2n-2 bars, as in Pine

    def test_weekly_monthly_resample(self):
        idx = pd.to_datetime(["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-12", "2026-10-30", "2026-11-02"])
        df = pd.DataFrame({"Open": [1, 2, 3, 4, 5, 6, 7, 8.0], "High": [2, 3, 4, 5, 9, 7, 8, 9.0], "Low": [1, 1, 2, 3, 4, 5, 6, 7.0],
                           "Close": [2, 3, 4, 5, 6, 7, 8, 9.0], "Volume": 10.0}, index=idx)
        w = ix.resample_htf(df, "weekly")
        self.assertEqual(w.iloc[0].to_dict(), {"Open": 1.0, "High": 9.0, "Low": 1.0, "Close": 6.0, "Volume": 50.0})
        self.assertEqual(len(w), 4)
        m = ix.resample_htf(df, "monthly")
        self.assertEqual((len(m), m.iloc[0]["Open"], m.iloc[0]["Close"], m.iloc[1]["Open"]), (2, 1.0, 8.0, 8.0))

    def test_htf_state_up_and_down(self):
        up = frame(path([(14, 110), (12, 104), (14, 120), (12, 112), (14, 130), (12, 122), (14, 140), (12, 130), (14, 150)], 100))
        dn = frame(path([(14, 90), (12, 96), (14, 80), (12, 88), (14, 70), (12, 78), (14, 60), (12, 70), (14, 50)], 100))
        self.assertEqual(ix.htf_state_series(up, 8, 3, 14)[0][-1], 1)
        self.assertEqual(ix.htf_state_series(dn, 8, 3, 14)[0][-1], -1)

    def test_extras(self):
        f = frame(path([(120, 200)], 100))
        out = ix.extras_latest(f, (("rsi", (("length", 14),)), ("macd", ()), ("volume_ratio", ())))
        self.assertGreater(out["rsi"], 90)
        self.assertGreater(out["macd"], 0)
        self.assertEqual(out["volume_ratio"], 1.0)


class TestConfig(unittest.TestCase):
    def test_repo_defaults(self):
        c = load_technical_config()
        self.assertEqual((c.ema_fast, c.ema_slow, c.sl_atr_len, c.sl_atr_mult, c.rr1, c.rr2, c.rr3), (9, 21, 14, 2.0, 1.0, 2.0, 3.0))
        self.assertEqual((c.atr_len, c.swing_atr_mult, c.pivot_left, c.pivot_right, c.impulse_min, c.adx_len), (14, 0.7, 8, 3, 55, 14))
        self.assertEqual((c.cross_lookback_bars, c.min_bars, c.stale_bar_days, c.min_rr_for_a), (3, 250, 7, 1.2))
        self.assertEqual(c.extras, ())

    def _yaml(self, body):
        p = Path(tempfile.mkdtemp()) / "i.yaml"
        p.write_text(body, encoding="utf-8")
        return p

    def test_core_cannot_be_disabled(self):
        with self.assertRaises(ConfigError):
            load_technical_config(self._yaml("indicators:\n  - {name: ema_fast, enabled: false}\n"))

    def test_unknown_indicator_rejected(self):
        with self.assertRaises(ConfigError):
            load_technical_config(self._yaml("indicators:\n  - {name: banana, enabled: true}\n"))

    def test_invalid_values_rejected(self):
        with self.assertRaises(ConfigError):
            load_technical_config(self._yaml("indicators:\n  - {name: ema_fast, params: {length: 30}}\n"))

    def test_optional_indicator_enabled(self):
        c = load_technical_config(self._yaml("indicators:\n  - {name: rsi, enabled: true, params: {length: 7}}\n"))
        self.assertEqual(c.extras, (("rsi", (("length", 7),)),))

    def test_parameters_are_editable(self):
        c = load_technical_config(self._yaml("indicators:\n  - {name: ema_fast, params: {length: 5}}\n  - {name: atr_risk, params: {length: 10, multiplier: 1.5}}\n"))
        self.assertEqual((c.ema_fast, c.sl_atr_len, c.sl_atr_mult), (5, 10, 1.5))


def build(segs, cfg=CFG, cut=0):
    fr = frame(path(BASE + UPTREND + segs))
    return run_engine(fr.iloc[:len(fr) - cut] if cut else fr, cfg)


class TestEngine(unittest.TestCase):
    def test_no_impulse_in_flat_market(self):
        r = run_engine(frame(path(BASE)), CFG)
        self.assertEqual((r.zone_side, r.valid_buy), (0, False))

    def test_short_series_does_not_crash(self):
        r = run_engine(frame(path([(15, 105)])), CFG)
        self.assertEqual(r.zone_side, 0)

    def test_wait_then_in_zone(self):
        w = build([(6, 134)])
        self.assertEqual((w.zone_side, w.z_state, w.price_in_zone), (1, 0, False))
        z = build([(8, 128)])
        self.assertEqual((z.z_state, z.price_in_zone), (1, True))
        self.assertTrue(z.z_lo < 128 < z.z_hi)

    def test_zone_is_fib_382_618_of_the_impulse(self):
        r = build([(8, 128)])
        rng = r.impulse_end - r.impulse_start
        self.assertAlmostEqual(r.z_hi, r.impulse_end - 0.382 * rng, places=6)
        self.assertAlmostEqual(r.z_lo, r.impulse_end - 0.618 * rng, places=6)
        self.assertAlmostEqual(r.chart_tp1, r.impulse_end + 0.272 * rng, places=6)
        self.assertAlmostEqual(r.chart_stop, r.impulse_start - 0.2 * r.atr_swing, places=6)

    def test_below_zone_above_stop_is_still_waiting(self):
        r = build([(10, 118)])
        self.assertEqual((r.z_state, r.price_in_zone), (0, False))

    def test_stopped_latches_even_if_price_returns_to_zone(self):
        r = build([(14, 110)])
        self.assertEqual(r.z_state, 3)
        r2 = build([(12, 110), (10, 127)])
        self.assertEqual((r2.z_state, r2.price_in_zone), (3, True))        # blown stop cannot revive

    def test_target_latches_even_if_price_returns_to_zone(self):
        r = build([(8, 128), (10, 149)])
        self.assertEqual(r.z_state, 2)
        r2 = build([(8, 128), (9, 148.5), (1, 140), (1, 127)])
        self.assertEqual((r2.z_state, r2.price_in_zone), (2, True))

    def test_new_impulse_replaces_the_old_setup(self):
        r = build([(8, 128), (10, 149), (12, 127)])
        self.assertGreater(r.impulse_end, 140)                              # anchored to the NEW (higher) impulse

    def test_impulse_threshold_controls_detection(self):
        strict = dataclasses.replace(CFG, impulse_min=101.0)
        self.assertEqual(build([(8, 128)], strict).zone_side, 0)

    def test_buy_trigger_and_cross_lookback(self):
        segs = [(6, 126), (20, 125), (8, 129.5)]
        one = dataclasses.replace(CFG, cross_lookback_bars=1)
        on_day = build(segs, one, cut=3)
        self.assertTrue(on_day.crossed_recent and on_day.price_in_zone and on_day.valid_buy)
        self.assertFalse(build(segs, one, cut=0).valid_buy)                 # original behaviour: signal only on the cross bar
        wide = dataclasses.replace(CFG, cross_lookback_bars=4)
        self.assertTrue(build(segs, wide, cut=0).valid_buy)                 # D5: still actionable 3 bars later while in zone
        self.assertFalse(build(segs, dataclasses.replace(CFG, cross_lookback_bars=2), cut=0).valid_buy)

    def test_deterministic_and_input_not_modified(self):
        fr = frame(path(BASE + UPTREND + [(8, 128)]))
        before = fr.copy()
        a, b = run_engine(fr, CFG), run_engine(fr, CFG)
        self.assertEqual(a, b)
        pd.testing.assert_frame_equal(fr, before)

    def test_htf_uses_last_completed_bar(self):
        h = make_history(seed=7, start="2018-01-01", end="2026-10-30")
        df = pd.DataFrame({"Open": h.Open, "High": h.High, "Low": h.Low, "Close": h.Close, "Volume": h.Volume})
        df.index = df.index.tz_localize(None)
        full = run_engine(df, CFG)                                          # last bar Fri 30 Oct
        part = run_engine(df.loc[:"2026-10-28"], CFG)                       # Wed of the same week
        self.assertEqual((full.weekly.bias, full.weekly.strength), (part.weekly.bias, part.weekly.strength))
        bars = ix.resample_htf(df, "weekly")
        trend, adx = ix.htf_state_series(bars, 8, 3, 14)
        self.assertEqual(full.weekly.bias, int(trend[len(bars) - 2]))        # Pine: t[1] of the bar holding the latest day
        self.assertEqual(full.weekly.strength, ix.adx_strength(adx[len(bars) - 2]))


def res(**kw):
    base = dict(bars=300, last_date="2026-10-30", close=100.0, atr_risk=2.0, atr_swing=2.0, ema_fast=1, ema_slow=1, crossed_recent=False,
                zone_side=1, z_state=0, z_hi=105.0, z_lo=95.0, chart_tp1=112.0, chart_stop=90.0, rr_mid=1.5, impulse_start=88.0,
                impulse_end=118.0, impulse_end_bar=290, price_in_zone=False, valid_buy=False, chart_trend=1, pivots=20)
    base.update(kw)
    return EngineResult(**base)


class TestSignals(unittest.TestCase):
    def test_state_mapping(self):
        cases = [(dict(zone_side=0), sg.S_FORMING), (dict(z_state=3), sg.S_VOID), (dict(z_state=2), sg.S_TARGET),
                 (dict(z_state=1, price_in_zone=True), sg.S_IN_ZONE), (dict(z_state=0), sg.S_WAIT),
                 (dict(z_state=1, price_in_zone=True, valid_buy=True), sg.S_TRIGGERED),
                 (dict(z_state=3, valid_buy=True), sg.S_VOID), (dict(zone_side=0, z_state=1), sg.S_FORMING)]
        for kw, want in cases:
            self.assertEqual(sg.state_text(res(**kw)), want, kw)

    def test_original_dashboard_regex_hides_forming_and_void(self):
        for s in (sg.S_FORMING, sg.S_VOID):
            self.assertRegex(s.upper(), "FORMING|VOID")

    def test_aligned_and_plan_wording(self):
        V = lambda b, s: HtfView(b, s, 100)
        cases = [([V(1, 60), V(1, 40)], "ALIGNED ▲ LONGS", sg.PLAN_BUY), ([V(1, 10), V(1, 15)], "ALIGNED ▲ LONGS", sg.PLAN_LOWCONV),
                 ([V(-1, 90), V(-1, 90)], "ALIGNED ▼ SHORTS", sg.PLAN_AVOID), ([V(-1, 5), V(-1, 5)], "ALIGNED ▼ SHORTS", sg.PLAN_AVOID),
                 ([V(1, 60), V(-1, 60)], "MIXED — WEAK EDGE", sg.PLAN_CAUTION), ([V(0, 0), V(0, 0)], "MIXED — WEAK EDGE", sg.PLAN_CAUTION),
                 ([V(1, 60), V(0, 0)], "▲ BULLISH BIAS", sg.PLAN_BUY), ([V(-1, 60), V(0, 0)], "▼ BEARISH BIAS", sg.PLAN_AVOID), ([], "NO HIGHER TF", sg.PLAN_CAUTION)]
        for views, aligned, plan in cases:
            m = sg.mtf_summary(views, CFG)
            self.assertEqual((m.aligned, m.plan), (aligned, plan), [(v.bias, v.strength) for v in views])

    def test_every_wording_pair_in_the_golden_alerts_is_reproducible(self):
        """All (aligned, plan) pairs found in the 217 stored production alerts must be producible by mtf_summary."""
        import itertools
        import json
        gold = json.loads((ROOT / "golden_master/original_outputs/technical_trading_alerts.json").read_text(encoding="utf-8"))
        wanted = {(g["aligned"], g["plan"]) for g in gold}
        made = set()
        for b1, b2, s1, s2 in itertools.product((-1, 0, 1), (-1, 0, 1), (0, 5, 15, 25, 60, 100), (0, 5, 15, 25, 60, 100)):
            m = sg.mtf_summary([HtfView(b1, s1, 9), HtfView(b2, s2, 9)], CFG)
            made.add((m.aligned, m.plan))
        self.assertEqual(wanted - made, set(), "production wording not reproducible")
        self.assertEqual(len(wanted), 4)

    def test_conviction_is_mean_of_voters_only(self):
        m = sg.mtf_summary([HtfView(1, 80, 9), HtfView(0, 0, 9)], CFG)
        self.assertEqual(m.conviction, 80.0)
        m2 = sg.mtf_summary([HtfView(1, 30, 9), HtfView(1, 10, 9)], CFG)
        self.assertEqual((m2.conviction, m2.low_conv), (20.0, False))      # exactly the floor is NOT low conviction (Pine: < 20)

    def test_levels_exact(self):
        lv = sg.levels(res(close=100.0, atr_risk=2.0), CFG)
        self.assertEqual(lv, {"entry": 100.0, "sl": 96.0, "tp1": 104.0, "tp2": 108.0, "tp3": 112.0})
        lv2 = sg.levels(res(close=487.7, atr_risk=13.26), CFG)
        self.assertEqual((lv2["entry"], lv2["sl"], lv2["tp1"], lv2["tp3"]), (487.7, 461.18, 514.22, 567.26))   # matches the AARTIIND golden row

    def test_rr_at_close(self):
        self.assertEqual(sg.rr_at_close(res(close=100, chart_stop=90, chart_tp1=115)), 1.5)
        self.assertIsNone(sg.rr_at_close(res(close=100, chart_stop=100, chart_tp1=115)))
        self.assertIsNone(sg.rr_at_close(res(zone_side=0)))

    def test_grades(self):
        G = lambda st, plan, rr=1.5, stale=False: sg.grade(st, plan, rr, stale, CFG)[0]
        self.assertEqual(G(sg.S_IN_ZONE, sg.PLAN_BUY), "A")
        self.assertEqual(G(sg.S_TRIGGERED, sg.PLAN_BUY, 1.2), "A")
        self.assertEqual(G(sg.S_IN_ZONE, sg.PLAN_BUY, 1.19), "B")
        self.assertEqual(G(sg.S_IN_ZONE, sg.PLAN_BUY, None), "B")
        self.assertEqual(G(sg.S_IN_ZONE, sg.PLAN_LOWCONV), "B")
        self.assertEqual(G(sg.S_TRIGGERED, sg.PLAN_CAUTION), "B")
        self.assertEqual(G(sg.S_IN_ZONE, sg.PLAN_AVOID), "")
        self.assertEqual(G(sg.S_WAIT, sg.PLAN_BUY), "C")
        self.assertEqual(G(sg.S_WAIT, sg.PLAN_CAUTION), "C")
        self.assertEqual(G(sg.S_WAIT, sg.PLAN_AVOID), "")
        for st in (sg.S_FORMING, sg.S_VOID, sg.S_TARGET):
            self.assertEqual(G(st, sg.PLAN_BUY), "")
        self.assertEqual(G(sg.S_IN_ZONE, sg.PLAN_BUY, 2.0, stale=True), "")
        self.assertTrue(all(sg.grade(s, sg.PLAN_BUY, 1.5, False, CFG)[1] for s in (sg.S_IN_ZONE, sg.S_WAIT, sg.S_FORMING)))   # always a reason

    def test_sort_order(self):
        mk = lambda a, g, rr, age: {"action": a, "details": {"grade": g, "rr_at_close": rr, "setup_age_bars": age}}
        rows = [mk(sg.S_WAIT, "C", 2.0, 5), mk(sg.S_IN_ZONE, "A", 1.3, 9), mk(sg.S_TRIGGERED, "A", 1.2, 3), mk(sg.S_IN_ZONE, "A", 1.8, 4),
                mk(sg.S_IN_ZONE, "B", 2.0, 1), mk(sg.S_FORMING, "", None, None)]
        out = sorted(rows, key=sg.sort_key)
        self.assertEqual([(r["action"], r["details"]["grade"], r["details"]["rr_at_close"]) for r in out],
                         [(sg.S_TRIGGERED, "A", 1.2), (sg.S_IN_ZONE, "A", 1.8), (sg.S_IN_ZONE, "A", 1.3), (sg.S_IN_ZONE, "B", 2.0),
                          (sg.S_WAIT, "C", 2.0), (sg.S_FORMING, "", None)])


if __name__ == "__main__":
    unittest.main()
