"""Integration: Technical + Fundamental + Shariah on ONE universe through ONE Data Manager, then the cross-screener view.
Proves the core consolidation promise: a download made for one screener is reused by the others."""
import json
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from fakes import FakeClock, FakeProvider  # noqa: E402
from mizan.data import DataManager  # noqa: E402
from mizan.overview import cross_view, load_results, write_cross_view  # noqa: E402
from mizan.screeners import fundamental as fund  # noqa: E402
from mizan.screeners import shariah as shar  # noqa: E402
from mizan.screeners import technical as tech  # noqa: E402
from mizan.screeners.technical.config import TechnicalConfig  # noqa: E402
from mizan.universe import UniverseService  # noqa: E402

NOW = datetime(2026, 11, 3, 10, 0, tzinfo=timezone.utc)
NAMES = ["AAA", "BBB", "CCC", "DDD", "EEE"]
FAST = {"throttle": {"requests_per_minute": 0, "max_retries": 0, "breaker_threshold": 3}}


class TestThreeScreenersTogether(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.p = FakeProvider()
        for i, n in enumerate(NAMES):
            cls.p.add(n, seed=10 + i)
        (cls.tmp / "u.txt").write_text("\n".join(NAMES) + "\n", encoding="utf-8")
        uni = UniverseService(cls.tmp / "u.txt").get()
        cls.store = cls.tmp / "store"
        cls.dm = DataManager(cls.p, cls.store, FAST, clock=FakeClock(), sleep=lambda s: None)
        t = tech.scan_universe(uni.tickers, cls.dm, TechnicalConfig(), now=NOW)
        tech.write_outputs(t, cls.store / "results" / "technical")
        f = fund.scan_universe(uni.tickers, cls.dm, fund.FundamentalParams(), now=NOW)
        fund.write_outputs(f, cls.store / "results" / "fundamental")
        s = shar.scan_universe(uni.tickers, cls.dm, shar.BseMapper(), shar.EvidenceStore(cls.store / "state" / "ev"), shar.ShariahParams(), now=NOW)
        shar.write_outputs(s, cls.store / "results" / "shariah", now=NOW)
        cls.t, cls.f, cls.s = t, f, s

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_every_screener_ran_on_every_stock(self):
        self.assertEqual((len(self.t.records), len(self.f.rows), len(self.s.records)), (5, 5, 5))
        self.assertEqual((self.t.failures, self.t.skipped, self.f.failed, self.s.failed), ({}, {}, {}, {}))

    def test_each_dataset_is_downloaded_once_for_all_three_screeners(self):
        dl = dict(self.dm.ledger.downloads)
        self.assertEqual(dl, {"ohlcv": 5, "info": 5, "statements": 5})       # not 10 / 15: Technical+Fundamental share prices,
        self.assertEqual(len(self.p.calls), 5)                                  # Fundamental+Shariah share info and statements
        self.assertEqual((self.p.info_calls, self.p.stmt_calls), (5, 5))
        self.assertGreaterEqual(sum(self.dm.ledger.hits.values()), 15)          # the other screeners were served from the cache

    def test_no_duplicate_downloads_in_the_ledger(self):
        self.assertTrue(self.dm.ledger.duplicates_for_day("2026-11-03").empty)
        counts = self.dm.ledger.counts_for_day("2026-11-03")
        self.assertTrue((counts["download_count"] == 1).all())

    def test_technical_uses_raw_prices_and_fundamental_adjusted_from_the_same_dataset(self):
        raw_last = float(self.p.data["AAA.FK"]["Close"].iloc[-1])
        tech_row = next(r for r in self.t.records if r["ticker"] == "AAA")
        fund_row = next(r for r in self.f.rows if r["Ticker"] == "AAA")
        self.assertEqual(tech_row["entry"], round(raw_last, 2))                  # raw
        self.assertAlmostEqual(fund_row["Current price"], round(raw_last * 0.98, 2), places=1)   # adjusted (Adj Close = 0.98 x Close)

    def test_cross_view_joins_by_ticker_without_mixing(self):
        res = load_results(self.store)
        self.assertEqual((len(res["technical"]), len(res["fundamental"]), len(res["shariah"])), (5, 5, 5))
        view = write_cross_view(self.store)
        self.assertEqual(sorted(r["ticker"] for r in view["rows"]), NAMES)
        for r in view["rows"]:
            self.assertTrue(r["technical"] and r["fundamental"] and r["shariah"])
            # the fake statements show debt = 44 % of assets: Criterion 3 is a breach, which settles the verdict even though the
            # other criteria are unknown - and nothing becomes COMPLIANT without evidence
            self.assertEqual(r["shariah"]["verdict"], "NON_COMPLIANT")
            self.assertEqual(r["shariah"]["breached"], ["Criteria 3: Debt / Assets"])
            self.assertIn("Criteria 1: Core Business", r["shariah"]["pending"])
        self.assertEqual(view["summary"]["technical_actionable_and_shariah_compliant"], [])
        self.assertTrue((self.store / "results" / "cross_view.json").exists())
        json.loads((self.store / "results" / "cross_view.json").read_text(encoding="utf-8"))


class TestCrossViewLogic(unittest.TestCase):
    T = [{"ticker": "AAA", "action": "🔵 IN BUY ZONE", "entry": 10, "sl": 9, "tp1": 11, "plan": "BUY pullbacks",
          "details": {"grade": "B", "changed_today": False, "notify_price": 10}},
         {"ticker": "BBB", "action": "🟢 BUY TRIGGERED", "entry": 20, "sl": 18, "tp1": 22, "plan": "BUY pullbacks",
          "details": {"grade": "A", "changed_today": True, "notify_price": 20}},
         {"ticker": "CCC", "action": "⚪ FORMING", "entry": 5, "sl": 4, "tp1": 6, "plan": "x", "details": {"grade": "", "changed_today": False}},
         {"ticker": "DDD", "action": "🟡 WAIT FOR BUY ZONE", "entry": 7, "sl": 6, "tp1": 8, "plan": "x",
          "details": {"grade": "C", "changed_today": True, "notify_price": 7.5}}]
    F = [{"Ticker": "AAA", "Status": "👀 WATCH", "Tech Score": 4, "Drawdown %": 40}, {"Ticker": "ZZZ", "Status": "WAIT (Fund)"}]
    S = [{"ticker": "AAA", "verdict": "COMPLIANT", "breached": [], "undetermined": []},
         {"ticker": "BBB", "verdict": "UNDETERMINED", "breached": [], "undetermined": [{"criterion": "Criteria 1: Core Business", "reason": "x"}]},
         {"ticker": "DDD", "verdict": "NON_COMPLIANT", "breached": ["Criteria 3: Debt / Assets"], "undetermined": []},
         {"ticker": "OLD", "is_compliant": True}]                                   # a legacy record without 'verdict'

    def test_join_order_matrix_and_independence(self):
        v = cross_view(self.T, self.F, self.S)
        order = [r["ticker"] for r in v["rows"]]
        self.assertEqual(order[:3], ["BBB", "DDD", "AAA"])                           # actionable first: changed today, then by grade
        self.assertEqual(v["summary"]["technical_actionable"], 3)
        self.assertEqual(v["summary"]["technical_actionable_by_shariah"], {"UNDETERMINED": 1, "NON_COMPLIANT": 1, "COMPLIANT": 1})
        self.assertEqual(v["summary"]["technical_actionable_and_shariah_compliant"], ["AAA"])
        self.assertEqual(v["summary"]["technical_actionable_shariah_undetermined"], ["BBB"])
        by = {r["ticker"]: r for r in v["rows"]}
        self.assertIsNone(by["ZZZ"]["technical"])                                    # stocks missing from a screener stay visible
        self.assertEqual(by["OLD"]["shariah"]["verdict"], "COMPLIANT")               # legacy record mapped, not invented
        self.assertEqual(by["BBB"]["shariah"]["pending"], ["Criteria 1: Core Business"])

    def test_screener_data_is_passed_through_untouched(self):
        v = cross_view(self.T, self.F, self.S)
        r = next(x for x in v["rows"] if x["ticker"] == "DDD")
        self.assertEqual((r["technical"]["grade"], r["technical"]["notify_price"], r["technical"]["entry"]), ("C", 7.5, 7))

    def test_missing_files_give_empty_view(self):
        d = Path(tempfile.mkdtemp())
        try:
            self.assertEqual(load_results(d), {"technical": [], "fundamental": [], "shariah": []})
            self.assertEqual(cross_view([], [], [])["summary"]["stocks"], 0)
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
