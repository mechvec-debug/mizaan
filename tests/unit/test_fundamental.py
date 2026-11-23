"""Phase 6 tests - Quality Value Rebound (Screener 2). Parity with the ORIGINAL scanner + new data-path behaviour.
Run: python -m unittest discover -s tests -v"""
import json
import math
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(Path(__file__).parent))

import harness  # noqa: E402  (loads the ORIGINAL code with network libraries stubbed)
from fakes import FakeClock  # noqa: E402
from fixture_builders import fin  # noqa: E402
from mizan.core.errors import PermanentProviderError, RateLimitError  # noqa: E402
from mizan.data import DataManager  # noqa: E402
from mizan.screeners.fundamental import (OUTPUT_COLUMNS, STATUS_ORDER, FundamentalParams, evaluate, run, scan_universe,  # noqa: E402
                                         technicals, write_outputs)
from mizan.universe import UniverseService  # noqa: E402

CH = ROOT / "golden_master" / "characterization"
FAST = {"throttle": {"requests_per_minute": 0, "max_retries": 0, "breaker_threshold": 3, "batch_size": 50}}
P = FundamentalParams()
NOW = datetime(2026, 11, 3, 10, 0, tzinfo=timezone.utc)


def J(name):
    with open(CH / name, encoding="utf-8") as fh:
        return json.load(fh)


def norm(o):
    return json.loads(json.dumps(o, default=lambda x: "NaN" if (isinstance(x, float) and math.isnan(x)) else str(x)))


def hist_frame(h):
    return pd.DataFrame({k: h[k] for k in ["Open", "High", "Low", "Close", "Volume"]}, index=pd.to_datetime(h["dates"]))


class ScenarioProvider:
    """Serves one fixture scenario per symbol through the real DataManager path (raw bars with AdjClose == Close)."""
    name = "scenario"

    def __init__(self):
        self.s, self.calls = {}, []

    def add(self, sc):
        self.s[sc["name"] + ".SC"] = sc

    def provider_symbol(self, t):
        return t + ".SC"

    def fetch_ohlcv(self, symbol, start, end):
        h = self.s[symbol]["hist"]
        df = pd.DataFrame({"Open": h["Open"], "High": h["High"], "Low": h["Low"], "Close": h["Close"], "Adj Close": h["Close"],
                           "Volume": h["Volume"]}, index=pd.to_datetime(h["dates"]).tz_localize("Asia/Kolkata"))
        if start is not None:
            df = df[df.index >= pd.Timestamp(start).tz_localize(df.index.tz)]
        return df

    def fetch_ohlcv_batch(self, symbols, start, end):
        return {s: self.fetch_ohlcv(s, start, end) for s in symbols}

    def fetch_info(self, symbol):
        return dict(self.s[symbol]["info"])

    def fetch_statements(self, symbol):
        ib = self.s[symbol]["income_balance"]
        if ib is None:
            return {"income_stmt": None, "balance_sheet": None, "financials": None}
        inc, bal = fin() if ib == "default" else fin(**ib)
        return {"income_stmt": inc, "balance_sheet": bal, "financials": None}


def dm_for(provider, tmp, last_bar_date):
    clock = FakeClock(start=(pd.Timestamp(last_bar_date) + pd.Timedelta(days=1)).strftime("%Y-%m-%dT10:00:00+00:00"))
    return DataManager(provider, tmp, FAST, clock=clock, sleep=lambda s: None), clock


class TestParityThroughDataManager(unittest.TestCase):
    """Fixture scenarios were produced by running the ORIGINAL scan_ticker; the new path must reproduce each row exactly."""

    def test_every_scenario_matches_the_original_row(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            sc_list = J("fundamental_scan_ticker_cases.json")["scenarios"]
            self.assertGreaterEqual(len(sc_list), 17)
            statuses = set()
            for sc in sc_list:
                prov = ScenarioProvider(); prov.add(sc)
                dm, clock = dm_for(prov, tmp / sc["name"], sc["hist"]["dates"][-1])
                res = scan_universe([sc["name"]], dm, P, now=clock())
                if sc["expected_row"] is None:
                    self.assertEqual(res.rows, [], sc["name"])
                    self.assertIn(sc["name"], res.skipped, sc["name"])
                    continue
                self.assertEqual(res.failed, {}, sc["name"])
                got = dict(res.rows[0]); got.pop("Updated")
                want = dict(sc["expected_row"])
                self.assertEqual(norm(got), norm(want), sc["name"])
                self.assertEqual(list(res.rows[0]), OUTPUT_COLUMNS, sc["name"])
                statuses.add(got["Status"])
            self.assertEqual(statuses, set(STATUS_ORDER))                       # all five statuses exercised
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_technicals_match_saved_original_output(self):
        for c in J("fundamental_technicals_cases.json")["cases"]:
            got = technicals(hist_frame(c["hist"]), P)
            for k, v in c["expected"].items():
                self.assertAlmostEqual(float(got[k]), float(v), places=9, msg=f"{c['name']}.{k}")


class TestDifferentialAgainstOriginalCode(unittest.TestCase):
    """200 randomised cases run through the ORIGINAL scan_ticker and the new evaluate(): outputs must be identical."""

    @classmethod
    def setUpClass(cls):
        cls.fw = harness.load_fundamental()

    @staticmethod
    def _hist(rng, n):
        knots = sorted(rng.uniform(0, 1, 3))
        xs = np.array([0, *knots, 1.0]) * (n - 1)
        ys = np.log(rng.uniform(40, 130, 5))
        close = np.round(np.exp(np.interp(np.arange(n), xs, ys) + rng.normal(0, 0.012, n).cumsum() * 0.25), 4)
        high, low = np.round(close * (1 + np.abs(rng.normal(0, .008, n))), 4), np.round(close * (1 - np.abs(rng.normal(0, .008, n))), 4)
        vol = rng.integers(80_000, 120_000, n).astype(float)
        vol[-12:] = np.round(vol[-12:] * rng.uniform(0.6, 2.0))
        return pd.DataFrame({"Open": close, "High": high, "Low": low, "Close": close, "Volume": vol}, index=pd.bdate_range("2023-11-01", periods=n))

    GOOD = dict(sector="Industrials", marketCap=5000e7, revenueGrowth=0.18, operatingMargins=0.15, returnOnEquity=0.18,
                debtToEquity=40.0, trailingPE=18.0, trailingEps=12.0, earningsGrowth=0.2, pegRatio=1.2)
    NEAR = dict(marketCap=[999e7, 1000e7, 1001e7], revenueGrowth=[.0799, .08, .0801], operatingMargins=[.0799, .08, .0801],
                returnOnEquity=[.0999, .1, .1001], debtToEquity=[99.9, 100, 100.1], trailingPE=[34.99, 35, 35.01, 0],
                trailingEps=[-1, 0, 8, 10, 12], pegRatio=[1.99, 2.0, 2.01, 0, None], sector=["Financial Services", None])

    @staticmethod
    def _shape_hist(rng, n=750):
        """Healthy-business price shapes: bottoming / rally-from-bottom (PASS-capable) / falling / near-high / pullback."""
        kind = int(rng.integers(5))
        if kind == 0:
            knots = [(0, 60), (.35, 100), (.72, 47), (.82, 46.5), (.9, 49.5), (1, 55.5)]
        elif kind == 1:
            lf, top, end = float(rng.choice([.70, .76, .80])), float(rng.choice([58, 62, 66])), float(rng.choice([54, 56, 58]))
            knots = [(0, 60), (.35, 100), (lf, 46), (lf + .06, 46.8), (.93, top), (1, end)]
        elif kind == 2:
            knots = [(0, 80), (.3, 100), (1, 52)]
        elif kind == 3:
            knots = [(0, 60), (.5, 85), (1, 98)]
        else:
            knots = [(0, 60), (.6, 100), (1, 85)]
        xs, ys = np.array([k[0] for k in knots]) * (n - 1), np.log([k[1] for k in knots])
        close = np.round(np.exp(np.interp(np.arange(n), xs, ys) + rng.normal(0, .012, n).cumsum() * .25), 4)
        high, low = np.round(close * (1 + np.abs(rng.normal(0, .008, n))), 4), np.round(close * (1 - np.abs(rng.normal(0, .008, n))), 4)
        vol = rng.integers(80_000, 120_000, n).astype(float)
        vol[-12:] = np.round(vol[-12:] * 1.6)
        return pd.DataFrame({"Open": close, "High": high, "Low": low, "Close": close, "Volume": vol}, index=pd.bdate_range("2023-11-01", periods=n))

    def _case(self, rng, i):
        pick = lambda lst: lst[int(rng.integers(len(lst)))]
        if i % 2 == 0:                                   # fully random inputs (explores failure branches)
            hist = self._hist(rng, int(rng.integers(120, 760)))
            info = dict(sector=pick(["Industrials", "Financial Services", "Technology", None]),
                        marketCap=pick([900e7, 999e7, 1000e7, 1001e7, 5000e7, 80000e7, None]),
                        revenueGrowth=pick([.07, .08, .0801, .15, -.1, None]), operatingMargins=pick([.07, .08, .0801, .2, None]),
                        returnOnEquity=pick([.05, .1, .1001, .2]), debtToEquity=pick([20, 99.9, 100, 100.1, 400, None]),
                        trailingPE=pick([0, 10, 35, 35.01, 60, float("nan")]), forwardPE=pick([0, 20, 40]),
                        trailingEps=pick([-1, 0, 8, 12, 15]), earningsGrowth=pick([-.1, .1, None]),
                        pegRatio=pick([0, 1.5, 2.0, 2.01, 4, None]))
        else:                                            # healthy business, 0-2 fields moved next to a threshold
            hist, info = self._shape_hist(rng), dict(self.GOOD)
            for key in rng.choice(list(self.NEAR), size=int(rng.integers(0, 3)), replace=False):
                info[str(key)] = pick(self.NEAR[str(key)])
        ib = pick(["default", "default", "default", "none", "low_roe", "eps_down", "no_eps"])
        if ib == "default":
            inc, bal = fin()
        elif ib == "none":
            inc, bal = None, None
        elif ib == "low_roe":
            inc, bal = fin(ni=20e7)
        elif ib == "eps_down":
            inc, bal = fin(eps=(9.0, 8.0))
        else:
            inc, bal = fin()
            inc = inc.drop(index="Diluted EPS")
        return hist, info, inc, bal

    def test_random_cases_identical(self):
        rng = np.random.default_rng(2026)
        seen = {}
        for i in range(400):
            hist, info, inc, bal = self._case(rng, i)
            sym = f"R{i}.NS"
            harness.FakeTicker.registry[sym] = harness.FakeTicker(sym, info=info, hist=hist, income=inc, balance=bal)
            want = self.fw.scan_ticker(sym)
            got = evaluate(sym, info, hist.dropna(subset=["Close", "High", "Low"]), inc, bal, NOW, P)
            want = {k: v for k, v in want.items() if k != "Updated"}
            got = {k: v for k, v in got.items() if k != "Updated"}
            self.assertEqual(norm(got), norm(want), f"case {i}")
            seen[got["Status"]] = seen.get(got["Status"], 0) + 1
        self.assertGreaterEqual(len(seen), 4, seen)           # the random space really reaches most statuses
        self.assertIn("WAIT (Fund)", seen)
        self.assertTrue({"👀 WATCH", "WAIT (Price)"} & set(seen), seen)       # and the interesting branches, not just failures
        print("\n  differential coverage:", seen)


class TestScannerBehaviour(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        sc = {s["name"]: s for s in J("fundamental_scan_ticker_cases.json")["scenarios"]}
        self.sc = sc
        names = ["pass_deep_value", "watch_falling_knife", "wait_price_near_high", "wait_valuation_pe_80", "wait_fund_low_roe"]
        self.names = names
        self.prov = ScenarioProvider()
        for n in names:
            self.prov.add(sc[n])
        self.last = sc["pass_deep_value"]["hist"]["dates"][-1]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def dm(self, sub="a"):
        return dm_for(self.prov, self.tmp / sub, self.last)

    def test_sort_order_and_output_files(self):
        dm, clock = self.dm()
        res = scan_universe(self.names, dm, P, now=clock())
        self.assertEqual([r["Status"] for r in res.rows], ["🔥 PASS", "👀 WATCH", "WAIT (Price)", "WAIT (Valuation)", "WAIT (Fund)"])
        paths = write_outputs(res, self.tmp / "out")
        data = json.loads(Path(paths["data"]).read_text(encoding="utf-8"))
        self.assertEqual(list(data[0]), OUTPUT_COLUMNS)
        self.assertTrue(Path(paths["data"]).read_text().startswith('[\n    {'))      # original data.json indent=4 format
        self.assertEqual(json.loads(Path(paths["report"]).read_text())["status"], "ok")

    def test_one_failing_ticker_does_not_stop_the_others(self):
        orig = self.prov.fetch_info

        def info(symbol):
            if symbol.startswith("watch_falling_knife"):
                raise PermanentProviderError("no such symbol")
            return orig(symbol)
        self.prov.fetch_info = info
        dm, clock = self.dm()
        res = scan_universe(self.names, dm, P, now=clock())
        self.assertEqual(len(res.rows), 4)
        self.assertIn("watch_falling_knife", res.failed)
        self.assertEqual(res.meta["status"], "partial")

    def test_statements_failure_degrades_like_the_original_and_is_reported(self):
        def boom(symbol):
            raise PermanentProviderError("no statements")
        self.prov.fetch_statements = boom
        dm, clock = self.dm()
        res = scan_universe(["pass_deep_value"], dm, P, now=clock())
        self.assertEqual(len(res.rows), 1)                                   # still evaluated (original behaviour)
        self.assertIn("pass_deep_value", res.degraded)                       # ...but never silent
        self.assertEqual(res.rows[0]["Status"], "WAIT (Fund)")               # empty statements => ROCE 0 => fund fail

    def test_stale_price_is_failed_not_published(self):
        dm, clock = self.dm()
        clock.advance(days=30)                                               # last bar is now 31 days old
        res = scan_universe(["pass_deep_value"], dm, P, now=clock())
        self.assertEqual(res.rows, [])
        self.assertTrue(res.failed["pass_deep_value"].startswith("stale_price"))

    def test_carry_forward_keeps_old_row_with_old_timestamp(self):
        dm, clock = self.dm("a")
        first = scan_universe(self.names, dm, P, now=clock())
        write_outputs(first, self.tmp / "out")
        prev = json.loads((self.tmp / "out" / "data.json").read_text())
        e = RateLimitError("429"); e.sticky = True
        self.prov.fetch_info = lambda s: (_ for _ in ()).throw(e)
        dm2, clock2 = self.dm("fresh_cache")                                  # empty cache => provider is hit and fails
        clock2.advance(days=2)
        res = scan_universe(self.names, dm2, P, now=clock2(), previous_rows=prev)
        self.assertEqual(sorted(res.carried), sorted(r["Ticker"] for r in prev))
        self.assertEqual(len(res.rows), 5)
        old = {r["Ticker"]: r["Updated"] for r in prev}
        self.assertTrue(all(r["Updated"] == old[r["Ticker"]] for r in res.rows))   # old time kept: cannot pass as fresh

    def test_carry_forward_expires(self):
        dm, clock = self.dm("a")
        first = scan_universe(self.names, dm, P, now=clock())
        write_outputs(first, self.tmp / "out")
        prev = json.loads((self.tmp / "out" / "data.json").read_text())
        self.prov.fetch_info = lambda s: (_ for _ in ()).throw(PermanentProviderError("gone"))
        dm2, clock2 = self.dm("fresh_cache")
        clock2.advance(days=10)
        res = scan_universe(self.names, dm2, P, now=clock2(), previous_rows=prev)
        self.assertEqual((res.carried, res.rows, res.meta["status"]), ([], [], "no_data"))

    def test_no_data_leaves_previous_output_untouched(self):
        dm, clock = self.dm("a")
        write_outputs(scan_universe(self.names, dm, P, now=clock()), self.tmp / "out")
        before = (self.tmp / "out" / "data.json").read_bytes()
        self.prov.fetch_info = lambda s: (_ for _ in ()).throw(PermanentProviderError("down"))
        dm2, clock2 = self.dm("fresh_cache")
        clock2.advance(days=30)
        paths = write_outputs(scan_universe(self.names, dm2, P, now=clock2()), self.tmp / "out")
        self.assertNotIn("data", paths)
        self.assertEqual((self.tmp / "out" / "data.json").read_bytes(), before)

    def test_removed_from_universe_means_removed_from_results(self):
        dm, clock = self.dm("a")
        write_outputs(scan_universe(self.names, dm, P, now=clock()), self.tmp / "out")
        prev = json.loads((self.tmp / "out" / "data.json").read_text())
        res = scan_universe(self.names[:3], self.dm("b")[0], P, now=clock(), previous_rows=prev)
        self.assertEqual({r["Ticker"] for r in res.rows}, set(self.names[:3]))

    def test_second_run_downloads_nothing_new(self):
        dm, clock = self.dm("a")
        scan_universe(self.names, dm, P, now=clock())
        n_dl = sum(dm.ledger.downloads.values())
        scan_universe(self.names, dm, P, now=clock())
        self.assertEqual(sum(dm.ledger.downloads.values()), n_dl)           # everything served from the shared cache

    def test_full_run_with_universe_service(self):
        alias = dict(zip(self.names, ["PASSDV", "WATCHFK", "WAITPNH", "WAITVPE", "WAITFLR"]))
        prov = ScenarioProvider()
        for n in self.names:
            prov.add(dict(self.sc[n], name=alias[n]))
        uni = self.tmp / "u.txt"
        uni.write_text("\n".join(alias.values()) + "\n", encoding="utf-8")
        dm, clock = dm_for(prov, self.tmp / "a", self.last)
        meta = run(UniverseService(uni).get(), dm, self.tmp / "res", P, now=clock())
        self.assertEqual((meta["status"], meta["evaluated"], meta["universe_count"]), ("ok", 5, 5))
        self.assertTrue(meta["universe_version"])
        self.assertTrue((self.tmp / "res" / "run_report.json").exists())
        rows = json.loads((self.tmp / "res" / "data.json").read_text())
        self.assertEqual({r["Ticker"] for r in rows}, set(alias.values()))


class TestRealUniverseMeetsGolden(unittest.TestCase):
    def test_golden_original_output_has_the_same_columns_and_sort_as_new_output(self):
        gold = json.loads((ROOT / "golden_master" / "original_outputs" / "fundamental_data.json").read_text(encoding="utf-8"))
        self.assertEqual(list(gold[0]), OUTPUT_COLUMNS)
        keys = [(STATUS_ORDER[r["Status"]], -r["Tech Score"]) for r in gold]
        self.assertEqual(keys, sorted(keys))


if __name__ == "__main__":
    unittest.main()
