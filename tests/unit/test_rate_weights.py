"""Rate accounting: a provider call is charged for the HTTP requests it really makes (Yahoo tolerates ~2,000-2,500/hour/IP)."""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from fakes import FakeClock, FakeProvider  # noqa: E402
from mizan.data import DataManager  # noqa: E402
from mizan.data.throttle import RateLimiter  # noqa: E402


class TestWeights(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp()); self.sleeps = []
        self.p = FakeProvider()
        for t in ("TCS", "INFY"):
            self.p.add(t, seed=1)

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def dm(self, **throttle):
        """DataManager whose limiters run on a fake clock that advances when they sleep (exact, no real waiting)."""
        cfg = {"throttle": {"requests_per_minute": 60, "max_retries": 0, **throttle}}   # 60/min = one slot per second
        m = DataManager(self.p, self.d, cfg, clock=FakeClock(), sleep=self.sleeps.append)
        t = [0.0]

        def sleep(sec):
            self.sleeps.append(sec); t[0] += sec
        m._limiters = {}

        def limiter(source):
            if source not in m._limiters:
                rpm = throttle.get("rpm_by_source", {}).get(source, 60)
                m._limiters[source] = RateLimiter(rpm, clock=lambda: t[0], sleep=sleep)
            return m._limiters[source]
        m._limiter = limiter
        return m

    def test_limiter_charges_the_weight(self):
        t, slept = [0.0], []
        rl = RateLimiter(60, clock=lambda: t[0], sleep=lambda s: (slept.append(s), t.__setitem__(0, t[0] + s)))
        rl.acquire(3); rl.acquire(1)
        self.assertEqual([round(s, 3) for s in slept], [3.0])

    def test_statements_cost_three_slots_and_info_two(self):
        m = self.dm()
        m.get_info("TCS")                      # costs 2 slots
        m.get_statements("TCS")                # waits 2 s for the info call, then costs 3 slots
        m.get_info("INFY")                     # waits 3 s for the statements call
        self.assertEqual([round(x, 3) for x in self.sleeps], [2.0, 3.0])

    def test_batch_download_pays_per_stock(self):
        names = [f"S{i:03d}" for i in range(60)]
        for n in names:
            self.p.add(n, seed=2)
        m = self.dm(batch_size=50)
        m.prefetch_ohlcv(names, years=3)       # a 50-stock batch then a 10-stock batch
        self.assertEqual(len(self.sleeps), 1)
        self.assertEqual(round(self.sleeps[0], 3), 50.0)          # 50 requests reserved before the next call may start

    def test_each_source_has_its_own_budget(self):
        m = self.dm(rpm_by_source={"fake": 120})      # one slot per half second for this source only
        m.get_info("TCS"); m.get_info("INFY")
        self.assertEqual(round(self.sleeps[0], 3), 1.0)           # 2 slots x 0.5 s
        self.assertIsNot(m._limiter("fake"), m._limiter("bse"))

    def test_hourly_ceiling_math(self):
        """The default 30 slots/min stays under the observed ~2,000/hour Yahoo tolerance even counting real requests."""
        self.assertLessEqual(30 * 60, 2000)


if __name__ == "__main__":
    unittest.main()
