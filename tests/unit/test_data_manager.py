"""Phase 4 tests - Data Acquisition Manager. Run: python -m unittest discover -s tests -v"""
import json
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from fakes import FakeClock, FakeProvider  # noqa: E402
from mizan.core.errors import (DataUnavailableError, PermanentProviderError, ProviderPausedError, RateLimitError,  # noqa: E402
                               TransientProviderError)
from mizan.data import CacheKey, DataManager, DownloadLedger  # noqa: E402
from mizan.data import keys as keys_mod, ohlcv as ox  # noqa: E402
from mizan.data.store import RawStore, safe_name  # noqa: E402
from mizan.data.throttle import CircuitBreaker, RateLimiter, retry_call  # noqa: E402

FAST = {"throttle": {"requests_per_minute": 0, "max_retries": 2, "base_delay_seconds": 2.0, "rate_limit_delay_seconds": 10.0,
                     "breaker_threshold": 3, "batch_size": 50}}


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.clock = FakeClock()
        self.p = FakeProvider().add("TCS", seed=1).add("INFY", seed=2)
        self.sleeps = []

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def mk(self, cfg=None, provider=None, run_id="r1"):
        c = json.loads(json.dumps(FAST))
        for k, v in (cfg or {}).items():
            c.setdefault(k, {}).update(v) if isinstance(v, dict) else c.__setitem__(k, v)
        return DataManager(provider or self.p, self.dir, c, run_id=run_id, clock=self.clock, sleep=self.sleeps.append, rng=lambda: 0.0)

    def n_calls(self):
        return len(self.p.calls)


class TestKeysAndHelpers(unittest.TestCase):
    def test_key_has_all_five_parts(self):
        k = CacheKey.make("TCS", "ohlcv", "yahoo", "1d")
        self.assertEqual(str(k), "ohlcv|yahoo|TCS|1d|v1")
        self.assertNotEqual(k.digest, CacheKey.make("TCS", "info", "yahoo", "snapshot").digest)
        self.assertNotEqual(k.digest, CacheKey.make("INFY", "ohlcv", "yahoo", "1d").digest)

    def test_safe_names(self):
        self.assertEqual(safe_name("^NSEI"), "_NSEI")
        self.assertEqual(safe_name("CON"), "CON_")
        self.assertEqual(safe_name("M&M"), "M&M")

    def test_clean_raw_tz_dupes_nan(self):
        idx = pd.DatetimeIndex(["2026-01-01 00:00", "2026-01-01 00:00", "2026-01-02", "2026-01-05"]).tz_localize("Asia/Kolkata")
        df = pd.DataFrame({"Open": 1, "High": 1, "Low": 1, "Close": [1.0, 2.0, np.nan, 3.0], "Adj Close": [1, 2, 2, 3],
                           "Volume": [1, 2, 3, 4], "Dividends": 0, "Stock Splits": 0}, index=idx)
        c = ox.clean_raw(df)
        self.assertEqual(list(c.index.strftime("%Y-%m-%d")), ["2026-01-01", "2026-01-05"])
        self.assertEqual(c["Close"].tolist(), [2.0, 3.0])                # duplicate: last wins; NaN close dropped
        self.assertIsNone(c.index.tz)

    def test_adjusted_view(self):
        raw = ox.clean_raw(pd.DataFrame({"Open": [10.0], "High": [12.0], "Low": [9.0], "Close": [10.0], "Adj Close": [5.0],
                                         "Volume": [100]}, index=pd.DatetimeIndex(["2026-01-01"])))
        a = ox.to_adjusted(raw)
        self.assertEqual((a["Close"].iloc[0], a["High"].iloc[0], a["Volume"].iloc[0]), (5.0, 6.0, 100))
        self.assertEqual(list(a.columns), ["Open", "High", "Low", "Close", "Volume"])


class TestThrottle(unittest.TestCase):
    def test_rate_limiter_spaces_calls(self):
        t, slept = [0.0], []
        rl = RateLimiter(60, clock=lambda: t[0], sleep=lambda s: (slept.append(s), t.__setitem__(0, t[0] + s)))
        for _ in range(3):
            rl.acquire()
        self.assertEqual([round(s, 3) for s in slept], [1.0, 1.0])

    def test_retry_backoff_and_permanent(self):
        slept, n = [], [0]

        def flaky():
            n[0] += 1
            if n[0] < 3:
                raise TransientProviderError("x")
            return "ok"
        v, attempts = retry_call(flaky, retries=3, base_delay=2, rate_limit_delay=10, sleep=slept.append, rng=lambda: 0.0)
        self.assertEqual((v, attempts, slept), ("ok", 3, [2, 4]))
        c = [0]

        def perm():
            c[0] += 1
            raise PermanentProviderError("bad symbol")
        with self.assertRaises(PermanentProviderError):
            retry_call(perm, retries=3, base_delay=1, rate_limit_delay=1, sleep=slept.append)
        self.assertEqual(c[0], 1)

    def test_rate_limit_uses_longer_delay(self):
        slept = []
        with self.assertRaises(RateLimitError):
            retry_call(lambda: (_ for _ in ()).throw(RateLimitError("429")), retries=2, base_delay=2, rate_limit_delay=10, sleep=slept.append, rng=lambda: 0.0)
        self.assertEqual(slept, [10, 20])

    def test_breaker(self):
        b = CircuitBreaker(2)
        b.record_rate_limit(); self.assertFalse(b.tripped)
        b.record_rate_limit(); self.assertTrue(b.tripped)
        b2 = CircuitBreaker(2); b2.record_rate_limit(); b2.record_success(); b2.record_rate_limit()
        self.assertFalse(b2.tripped)


class TestSharedCache(Base):
    def test_cold_start_then_hit(self):
        m = self.mk()
        r = m.get_ohlcv("TCS", years=3)
        self.assertEqual((r.status, r.actions, r.from_cache), ("fresh", ["full"], False))
        r2 = m.get_ohlcv("TCS", years=3)
        self.assertTrue(r2.from_cache)
        self.assertEqual(self.n_calls(), 1)

    def test_three_screeners_one_download(self):
        """Screener 1 (all history), Screener 2 (3y), Screener 3 (fundamentals needs none) share ONE download."""
        m = self.mk()
        a = m.get_ohlcv("TCS")                      # technical: everything stored
        b = m.get_ohlcv("TCS", years=3)             # fundamental: 3y window
        c = m.get_ohlcv("TCS", years=1, min_bars=100)
        self.assertEqual(self.n_calls(), 1)
        self.assertGreater(len(a.data), len(b.data))
        self.assertGreater(len(b.data), len(c.data))

    def test_second_process_reuses_store(self):
        self.mk(run_id="job-A").get_ohlcv("TCS", years=3)
        self.mk(run_id="job-B").get_ohlcv("TCS", years=3)       # new manager, same store folder
        self.assertEqual(self.n_calls(), 1)

    def test_ttl_boundary(self):
        m = self.mk()
        m.get_ohlcv("TCS")
        self.clock.advance(hours=19)
        m.get_ohlcv("TCS")
        self.assertEqual(self.n_calls(), 1)
        self.clock.advance(hours=2)
        self.p.extend_to("TCS", "2026-11-03")
        r = m.get_ohlcv("TCS")
        self.assertEqual(r.actions, ["incremental"])
        self.assertEqual(self.n_calls(), 2)

    def test_incremental_fetches_only_the_tail_and_appends(self):
        m = self.mk()
        first = m.get_ohlcv("TCS", adjusted=False)
        self.clock.advance(hours=24)
        self.p.extend_to("TCS", "2026-11-03")
        r = m.get_ohlcv("TCS", adjusted=False)
        call = self.p.calls[-1]
        self.assertEqual(str(call["start"]), "2026-10-26")                      # last bar 11-02 minus 7 overlap days
        self.assertIsNone(call["end"])
        self.assertEqual(len(r.data), len(first.data) + 1)
        pd.testing.assert_frame_equal(r.data.iloc[:len(first.data)], first.data)  # history untouched
        self.assertEqual(r.last_date, "2026-11-03")

    def test_backfill_fetches_only_missing_earlier_part(self):
        m = self.mk({"ohlcv": {"initial_history_years": 2}})
        r1 = m.get_ohlcv("TCS", years=2)
        self.assertEqual(r1.actions, ["full"])
        r2 = m.get_ohlcv("TCS", years=5)
        self.assertEqual(r2.actions, ["backfill"])
        call = self.p.calls[-1]
        self.assertEqual(str(call["start"]), "2021-11-03")
        self.assertTrue(pd.Timestamp(call["end"]) < pd.Timestamp("2024-12-01"))   # NOT a full re-download
        self.assertGreater(len(r2.data), len(r1.data))
        n = self.n_calls()
        m.get_ohlcv("TCS", years=5)
        self.assertEqual(self.n_calls(), n)

    def test_recent_ipo_not_requested_forever(self):
        self.p.add("NEWCO", start="2026-06-01", seed=5)
        m = self.mk()
        r = m.get_ohlcv("NEWCO", years=3)
        self.assertTrue(any("recent listing" in i for i in r.issues))
        n = self.n_calls()
        m.get_ohlcv("NEWCO", years=5)                                  # would normally trigger a backfill
        self.assertEqual(self.n_calls(), n)
        r3 = m.get_ohlcv("NEWCO", years=3, min_bars=200)
        self.assertEqual(r3.status, "partial")
        self.assertTrue(any("bars available" in i for i in r3.issues))

    def test_benchmark_symbol(self):
        self.p.data["^NSEI"] = self.p.data["TCS.FK"].copy()
        m = self.mk()
        m.get_ohlcv("^NSEI", years=1)
        self.assertTrue((self.dir / "raw" / "ohlcv" / "fake" / "_NSEI.csv").exists())

    def test_cache_version_bump_invalidates(self):
        m = self.mk()
        m.get_ohlcv("TCS")
        with mock.patch.dict(keys_mod.DATA_VERSIONS, {"ohlcv": "v2"}):
            self.mk(run_id="r2").get_ohlcv("TCS")
        self.assertEqual(self.n_calls(), 2)


class TestAdjustmentDrift(Base):
    def _setup_day2(self, m):
        m.get_ohlcv("TCS")
        self.clock.advance(hours=24)
        self.p.extend_to("TCS", "2026-11-03")

    def test_no_event_means_no_full_refresh(self):
        m = self.mk(); self._setup_day2(m)
        self.assertEqual(m.get_ohlcv("TCS").actions, ["incremental"])

    def test_dividend_triggers_full_refresh_and_data_matches_provider(self):
        m = self.mk(); self._setup_day2(m)
        self.p.apply_dividend("TCS", "2026-11-03", 0.97)
        r = m.get_ohlcv("TCS", adjusted=False)
        self.assertEqual(r.actions, ["full_refresh"])
        want = ox.clean_raw(self.p.data["TCS.FK"])
        want = want[want.index >= r.data.index[0]]
        pd.testing.assert_frame_equal(r.data, want, check_freq=False)

    def test_split_triggers_full_refresh(self):
        m = self.mk(); self._setup_day2(m)
        self.p.apply_split("TCS", "2026-11-03", 2.0)
        r = m.get_ohlcv("TCS", adjusted=False)
        self.assertEqual(r.actions, ["full_refresh"])
        prov = ox.clean_raw(self.p.data["TCS.FK"])
        self.assertAlmostEqual(r.data["Close"].iloc[0], prov["Close"].loc[r.data.index[0]], places=4)
        self.assertEqual(r.data["Close"].loc["2026-11-02"], prov["Close"].loc["2026-11-02"])


class TestFailures(Base):
    def test_transient_retried_with_backoff(self):
        self.p.errors["TCS.FK"] = [TransientProviderError("t"), TransientProviderError("t")]
        r = self.mk().get_ohlcv("TCS")
        self.assertEqual(r.status, "fresh")
        self.assertEqual(self.sleeps, [2.0, 4.0])
        led = DownloadLedger(self.dir / "ledger", "x").read()
        self.assertEqual(int(led.iloc[-1]["attempts"]), 3)

    def test_permanent_not_retried_and_not_hammered(self):
        e = PermanentProviderError("no such symbol"); e.sticky = True
        self.p.errors["TCS.FK"] = [e]
        m = self.mk()
        with self.assertRaises(DataUnavailableError) as cm:
            m.get_ohlcv("TCS")
        self.assertEqual(cm.exception.reason, "permanent")
        with self.assertRaises(DataUnavailableError) as cm2:
            m.get_ohlcv("TCS")
        self.assertEqual(cm2.exception.reason, "earlier_failure")
        self.assertEqual(self.n_calls(), 1)

    def test_empty_response_is_an_error_not_empty_data(self):
        with self.assertRaises(DataUnavailableError) as cm:
            self.mk().get_ohlcv("GHOST")
        self.assertEqual(cm.exception.reason, "no_data")

    def test_circuit_breaker_stops_hammering(self):
        for t in ("A", "B", "C", "D"):
            self.p.add(t, seed=3)
            e = RateLimitError("429"); e.sticky = True
            self.p.errors[t + ".FK"] = [e] * 5
        m = self.mk({"throttle": {"max_retries": 1}})
        for t in ("A", "B", "C"):
            with self.assertRaises(DataUnavailableError):
                m.get_ohlcv(t)
        before = self.n_calls()
        with self.assertRaises(ProviderPausedError):
            m.get_ohlcv("D")
        self.assertEqual(self.n_calls(), before)                        # provider not called while paused
        self.assertTrue(m.stats()["breaker_tripped"])

    def test_stale_cache_never_returned_silently(self):
        m = self.mk()
        good = m.get_ohlcv("TCS")
        self.clock.advance(hours=30)
        e = TransientProviderError("down"); e.sticky = True
        self.p.errors["TCS.FK"] = [e]
        with self.assertRaises(DataUnavailableError):
            m.get_ohlcv("TCS")
        r = self.mk(run_id="r2").get_ohlcv("TCS", allow_stale=True)
        self.assertEqual(r.status, "stale")
        self.assertTrue(r.issues)
        self.assertEqual(len(r.data), len(good.data))

    def test_old_last_bar_flagged(self):
        self.clock.advance(days=20)                  # provider has nothing newer than 2026-11-02
        r = self.mk().get_ohlcv("TCS")
        self.assertEqual(r.status, "stale")
        self.assertTrue(any("days old" in i for i in r.issues))


class TestRawProtection(Base):
    def test_screener_cannot_change_shared_data(self):
        m = self.mk()
        r = m.get_ohlcv("TCS", adjusted=False)
        path = self.dir / "raw" / "ohlcv" / "fake" / "TCS.csv"
        before = path.read_bytes()
        r.data.iloc[:, :] = 0                                           # screener scribbles on its copy
        r2 = m.get_ohlcv("TCS", adjusted=False)
        self.assertGreater(r2.data["Close"].min(), 0)
        self.assertEqual(path.read_bytes(), before)

    def test_reading_does_not_write(self):
        m = self.mk()
        m.get_ohlcv("TCS")
        path = self.dir / "raw" / "ohlcv" / "fake" / "TCS.csv"
        mt = path.stat().st_mtime_ns
        for _ in range(3):
            m.get_ohlcv("TCS", years=2)
        self.assertEqual(path.stat().st_mtime_ns, mt)

    def test_file_format_is_git_friendly(self):
        self.mk().get_ohlcv("TCS")
        b = (self.dir / "raw" / "ohlcv" / "fake" / "TCS.csv").read_bytes()
        self.assertNotIn(b"\r", b)
        self.assertTrue(b.startswith(b"Date,Open,High,Low,Close,AdjClose,Volume,Dividends,Splits\n"))
        self.assertEqual(list(self.dir.rglob("*.tmp")), [])

    def test_store_roundtrip_exact(self):
        m = self.mk()
        a = m.get_ohlcv("TCS", adjusted=False).data
        b = self.mk(run_id="r2").get_ohlcv("TCS", adjusted=False).data
        pd.testing.assert_frame_equal(a, b)


class TestInfoAndStatements(Base):
    def test_separate_ttls(self):
        m = self.mk()
        m.get_info("TCS"); m.get_statements("TCS")
        self.clock.advance(hours=30)
        m.get_info("TCS"); m.get_statements("TCS")
        self.assertEqual((self.p.info_calls, self.p.stmt_calls), (2, 1))
        self.clock.advance(hours=200)
        m.get_statements("TCS")
        self.assertEqual(self.p.stmt_calls, 2)

    def test_info_sanitised_and_cached(self):
        m = self.mk()
        r = m.get_info("TCS")
        self.assertIsNone(r.data["trailingPE"])                         # NaN -> null in storage and in the result
        r.data["marketCap"] = -1                                        # mutate the copy
        self.assertEqual(m.get_info("TCS").data["marketCap"], 5e10)
        self.assertTrue(m.get_info("TCS").from_cache)

    def test_statements_roundtrip(self):
        r = self.mk().get_statements("TCS")
        bs = r.data["balance_sheet"]
        self.assertIsNone(r.data["income_stmt"])
        self.assertEqual(list(bs.index), ["Total Assets", "Total Debt"])
        self.assertEqual(list(bs.columns), [pd.Timestamp("2026-03-31"), pd.Timestamp("2025-03-31")])
        self.assertEqual(bs.loc["Total Assets"].tolist(), [9e9, 8e9])
        self.assertTrue(np.isnan(bs.loc["Total Debt", pd.Timestamp("2025-03-31")]))

    def test_stale_info_flagged(self):
        m = self.mk()
        m.get_info("TCS")
        self.clock.advance(hours=30)
        e = TransientProviderError("down"); e.sticky = True
        self.p.errors["TCS.FK"] = [e]
        with self.assertRaises(DataUnavailableError):
            m.get_info("TCS")
        r = m.get_info("TCS", allow_stale=True) if False else self.mk(run_id="r2").get_info("TCS", allow_stale=True)
        self.assertEqual(r.status, "stale")


class TestConcurrency(Base):
    def test_threads_one_manager_one_download(self):
        self.p.delay = 0.05
        m = self.mk()
        out = []
        ts = [threading.Thread(target=lambda: out.append(m.get_ohlcv("TCS", years=3))) for _ in range(8)]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.assertEqual(len(out), 8)
        self.assertEqual(self.n_calls(), 1)

    def test_two_managers_same_store_one_download(self):
        self.p.delay = 0.05
        ms = [self.mk(run_id=f"job{i}") for i in range(2)]
        ts = [threading.Thread(target=lambda mm=mm: mm.get_ohlcv("INFY", years=3)) for mm in ms for _ in range(3)]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.assertEqual(self.n_calls(), 1)


class TestPrefetch(Base):
    def setUp(self):
        super().setUp()
        self.names = [f"S{i:03d}" for i in range(120)]
        for i, n in enumerate(self.names):
            self.p.add(n, seed=i)

    def test_batches_then_all_hits(self):
        m = self.mk()
        rep = m.prefetch_ohlcv(self.names, years=3)
        self.assertEqual((len(rep.downloaded), rep.failed), (120, {}))
        self.assertEqual(self.n_calls(), 3)                             # 120 tickers / batch of 50 = 3 provider calls
        for n in self.names:
            self.assertTrue(m.get_ohlcv(n, years=3).from_cache)
        self.assertEqual(self.n_calls(), 3)

    def test_second_prefetch_incremental_and_partial_failure(self):
        m = self.mk(); m.prefetch_ohlcv(self.names, years=3)
        self.clock.advance(hours=24)
        for n in self.names:
            self.p.extend_to(n, "2026-11-03")
        e = TransientProviderError("boom"); e.sticky = True
        self.p.errors["S007.FK"] = [e]
        n0 = self.n_calls()
        rep = m.prefetch_ohlcv(self.names, years=3)
        self.assertEqual(self.n_calls() - n0, 3)
        self.assertEqual(list(rep.failed), ["S007"])
        self.assertEqual(len(rep.downloaded), 119)
        self.assertEqual(m.get_ohlcv("S000").last_date, "2026-11-03")

    def test_up_to_date_cache_means_zero_calls(self):
        m = self.mk(); m.prefetch_ohlcv(self.names, years=3)
        n0 = self.n_calls()
        rep = m.prefetch_ohlcv(self.names, years=3)
        self.assertEqual((rep.cache_hits, self.n_calls() - n0), (120, 0))

    def test_rate_limit_on_batch_defers_rest(self):
        e = RateLimitError("429"); e.sticky = True
        orig = self.p.fetch_ohlcv_batch

        def limited(symbols, start, end):
            self.p.calls.append({"kind": "batch"})
            raise e
        self.p.fetch_ohlcv_batch = limited
        m = self.mk({"throttle": {"breaker_threshold": 1, "max_retries": 0}})
        rep = m.prefetch_ohlcv(self.names, years=3)
        self.assertEqual(self.n_calls(), 1)                             # first batch hit the limit -> breaker -> stop
        self.assertEqual(len(rep.failed), 50)
        self.assertEqual(len(rep.deferred), 70)


class TestLedger(Base):
    def test_ledger_rows_counts_and_duplicates(self):
        m = self.mk()
        m.get_ohlcv("TCS", years=3); m.get_ohlcv("INFY", years=3); m.get_info("TCS")
        m.get_ohlcv("TCS", years=3)                                      # cache hit: not a download
        led = m.ledger.counts_for_day("2026-11-03")
        self.assertEqual(sorted(led["ticker"] + ":" + led["data_type"]), ["INFY:ohlcv", "TCS:info", "TCS:ohlcv"])
        self.assertTrue((led["download_count"] == 1).all())
        self.assertTrue(m.ledger.duplicates_for_day("2026-11-03").empty)
        m.ledger.record(ticker="TCS", data_type="ohlcv", source="fake", period="1d", action="full")
        dup = m.ledger.duplicates_for_day("2026-11-03")
        self.assertEqual(len(dup), 1)

    def test_run_stats_file(self):
        m = self.mk()
        m.get_ohlcv("TCS"); m.get_ohlcv("TCS"); m.get_ohlcv("TCS")
        p = m.close()
        d = json.loads(p.read_text())
        self.assertEqual((d["downloads"]["ohlcv"], d["cache_hits"]["ohlcv"]), (1, 2))

    def test_size_report(self):
        m = self.mk(); m.get_ohlcv("TCS")
        rep = m.stats()["store"]
        self.assertGreater(rep["bytes"], 0)
        self.assertIn("ohlcv", rep["by_type_mb"])


if __name__ == "__main__":
    unittest.main()
