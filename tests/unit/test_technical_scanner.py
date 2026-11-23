"""Phase 5 tests - scanner, output schema, golden-data compatibility, dashboard filters."""
import json
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from fakes import FakeClock, FakeProvider  # noqa: E402
from mizan.core.errors import PermanentProviderError  # noqa: E402
from mizan.data import DataManager  # noqa: E402
from mizan.screeners.technical import ORIGINAL_COLUMNS, load_technical_config, scan_universe, write_outputs  # noqa: E402
from mizan.screeners.technical import filters  # noqa: E402

CFG = load_technical_config()
NOW = datetime(2026, 11, 3, 10, 0, tzinfo=timezone.utc)            # = 15:30 IST
GOLD = ROOT / "golden_master" / "original_outputs" / "technical_trading_alerts.json"
FAST = {"throttle": {"requests_per_minute": 0, "max_retries": 0}}


class ScanBase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.p = FakeProvider()
        for i, t in enumerate(["AAA", "BBB", "CCC", "DDD"]):
            self.p.add(t, seed=10 + i, start="2018-01-02")
        self.p.add("SHORT", seed=1, start="2026-01-02")                # ~215 bars  (< 250)
        self.p.add("OLDONE", seed=2, start="2018-01-02", end="2026-09-01")   # last bar 63 days before the clock
        self.dm = DataManager(self.p, self.dir, FAST, clock=FakeClock(), sleep=lambda s: None)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def scan(self, tickers=("AAA", "BBB", "CCC", "DDD"), **kw):
        return scan_universe(list(tickers), self.dm, CFG, now=NOW, **kw)


class TestScanner(ScanBase):
    def test_record_schema_matches_original_alert_format(self):
        res = self.scan()
        self.assertEqual(len(res.records), 4)
        for r in res.records:
            self.assertEqual(list(r)[:10], ORIGINAL_COLUMNS)
            self.assertEqual(list(r)[10:], ["details"])
            self.assertEqual(r["time_received"], "2026-11-03 15:30:00")           # D4: IST, same format
            self.assertRegex(r["action"], r"FORMING|VOID|TARGET|BUY|WAIT|ZONE")
            for k in ("entry", "sl", "tp1", "tp2", "tp3"):
                self.assertEqual(r[k], round(r[k], 2))

    def test_levels_follow_the_risk_multiple_rule_on_every_row(self):
        for r in self.scan().records:
            risk = r["entry"] - r["sl"]
            self.assertGreater(risk, 0)
            for k, m in (("tp1", 1), ("tp2", 2), ("tp3", 3)):
                self.assertAlmostEqual(r[k] - r["entry"], risk * m, delta=0.03)          # each level is rounded to 2 dp on its own (same as the golden rows)

    def test_golden_schema_compatibility(self):
        gold = json.loads(GOLD.read_text(encoding="utf-8"))
        self.assertEqual(list(gold[0]), ORIGINAL_COLUMNS)

    def test_short_history_skipped_and_reported(self):
        res = self.scan(("AAA", "SHORT"))
        self.assertEqual([r["ticker"] for r in res.records], ["AAA"])
        self.assertIn("SHORT", res.skipped)
        self.assertIn("< 250", res.skipped["SHORT"])

    def test_stale_data_never_graded(self):
        res = self.scan(("OLDONE",))
        d = res.records[0]["details"]
        self.assertEqual((d["data_status"], d["grade"]), ("stale", ""))
        self.assertIn("stale", d["reason"])

    def test_one_failure_does_not_stop_the_others(self):
        e = PermanentProviderError("gone"); e.sticky = True
        self.p.errors["BBB.FK"] = [e]
        res = self.scan(("AAA", "BBB", "CCC"))
        self.assertEqual(sorted(r["ticker"] for r in res.records), ["AAA", "CCC"])
        self.assertIn("BBB", res.failures)
        self.assertEqual(res.meta["failed"], 1)

    def test_engine_error_is_isolated(self):
        import mizan.screeners.technical.scanner as sc
        real = sc.run_engine
        sc.run_engine = lambda df, cfg: (_ for _ in ()).throw(ValueError("boom")) if df["Close"].iloc[0] == self.dm.get_ohlcv("BBB", adjusted=False).data["Close"].iloc[0] else real(df, cfg)
        try:
            res = self.scan(("AAA", "BBB"))
        finally:
            sc.run_engine = real
        self.assertIn("BBB", res.failures)
        self.assertIn("engine: ValueError", res.failures["BBB"])
        self.assertEqual([r["ticker"] for r in res.records], ["AAA"])

    def test_deterministic(self):
        a, b = self.scan(), self.scan()
        self.assertEqual(json.dumps(a.records, sort_keys=True), json.dumps(b.records, sort_keys=True))

    def test_records_sorted_by_grade_then_state(self):
        from mizan.screeners.technical.signals import sort_key
        res = self.scan()
        self.assertEqual(res.records, sorted(res.records, key=sort_key))

    def test_changed_today_and_state_since(self):
        first = self.scan()
        again = self.scan(previous=first.records)
        for r in again.records:
            self.assertFalse(r["details"]["changed_today"])
            self.assertEqual(r["details"]["state_since"], "2026-11-03")
        old = [dict(r, action="🟡 WAIT FOR BUY ZONE" if r["action"] != "🟡 WAIT FOR BUY ZONE" else "⚪ FORMING",
                    details=dict(r["details"], state_since="2026-10-01")) for r in first.records]
        moved = self.scan(previous=old)
        self.assertTrue(all(r["details"]["changed_today"] and r["details"]["prev_action"] for r in moved.records))
        carried = [dict(r, details=dict(r["details"], state_since="2026-10-01")) for r in first.records]
        self.assertTrue(all(r["details"]["state_since"] == "2026-10-01" for r in self.scan(previous=carried).records))

    def test_shared_cache_second_scan_downloads_nothing(self):
        self.scan()
        n = len(self.p.calls)
        self.scan()
        self.assertEqual(len(self.p.calls), n)

    def test_outputs_written(self):
        res = self.scan()
        paths = write_outputs(res, self.dir / "out", {"universe_version": "abc123"})
        recs = json.loads(Path(paths["alerts"]).read_text(encoding="utf-8"))
        rep = json.loads(Path(paths["report"]).read_text(encoding="utf-8"))
        self.assertEqual(len(recs), 4)
        self.assertEqual((rep["scanned"], rep["universe_version"]), (4, "abc123"))
        self.assertEqual(rep["cross_lookback_bars"], 3)

    def test_optional_indicators_become_extra_columns_only_when_enabled(self):
        import dataclasses
        cfg = dataclasses.replace(CFG, extras=(("rsi", (("length", 14),)), ("volume_ratio", ())))
        r = scan_universe(["AAA"], self.dm, cfg, now=NOW).records[0]
        self.assertEqual(sorted(r["details"]["extras"]), ["rsi", "volume_ratio"])
        self.assertEqual(self.scan(("AAA",)).records[0]["details"]["extras"], {})
        self.assertEqual(list(r)[:10], ORIGINAL_COLUMNS)                        # original columns untouched


class TestFilters(unittest.TestCase):
    def test_original_dashboard_logic_reproduces_golden_baseline(self):
        gold = json.loads(GOLD.read_text(encoding="utf-8"))
        base = json.loads((ROOT / "golden_master/characterization/technical_dashboard_filter_baseline.json").read_text(encoding="utf-8"))
        vis = filters.original_visible([dict(r, details={}) for r in gold])
        self.assertEqual(len(vis), base["rows_after_hidden_filters"])
        self.assertEqual(sorted(vis["ticker"]), base["tickers_after_filters"])
        self.assertEqual(list(vis.columns), ORIGINAL_COLUMNS)

    def test_views_split_by_grade(self):
        mk = lambda g, plan="BUY pullbacks": {"ticker": g or "x", "action": "a", "plan": plan, "details": {"grade": g}}
        v = filters.views([mk("A"), mk("B"), mk("C"), mk("", "AVOID: Macro Downtrend")])
        self.assertEqual((len(v["actionable"]), len(v["watch"]), len(v["all"])), (2, 1, 4))


class TestRawPrices(ScanBase):
    """D1: the Technical screener publishes RAW (not dividend-adjusted) prices - entry must be a real traded price."""

    def test_entry_is_the_raw_close_not_the_adjusted_close(self):
        rec = {r["ticker"]: r for r in self.scan().records}["AAA"]
        raw_close = float(self.p.data["AAA.FK"]["Close"].iloc[-1])
        adj_close = raw_close * 0.98                                     # FakeProvider: Adj Close = 0.98 x Close
        self.assertEqual(rec["entry"], round(raw_close, 2))
        self.assertNotAlmostEqual(rec["entry"], adj_close, places=1)


class TestNotificationPrice(ScanBase):
    def test_notify_price_rule_holds_on_every_record(self):
        recs = self.scan().records
        self.assertTrue(recs)
        for r in recs:
            d = r["details"]
            self.assertIn("notify_price", d)
            if "IN BUY ZONE" in r["action"] or "TRIGGERED" in r["action"]:
                self.assertEqual(d["notify_price"], r["entry"])                  # at the entry: act now
            elif "WAIT" in r["action"] and d["zone_hi"] is not None and r["entry"] > d["zone_hi"]:
                self.assertEqual(d["notify_price"], d["zone_hi"])                # alert at the top of the zone
            else:
                self.assertIsNone(d["notify_price"])


if __name__ == "__main__":
    unittest.main()
