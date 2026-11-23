"""Invariant checks on the three golden-master outputs. Run:  python -m unittest discover -s tests -v
These test that the ORIGINAL outputs obey the ORIGINAL rules. Violations are reported as KNOWN_ANOMALIES
(documented, never silently fixed)."""
import json, os, re, unittest, hashlib

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
GM = os.path.join(ROOT, "golden_master", "original_outputs")
def _j(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def load(n):
    return _j(os.path.join(GM, n))

STATUS_ORDER = {"🔥 PASS": 0, "👀 WATCH": 1, "WAIT (Price)": 2, "WAIT (Valuation)": 3, "WAIT (Fund)": 4}
FUND_COLUMNS = ["Ticker", "Market Cap", "Current price", "PE", "OPM", "Sales growth", "ROE", "ROCE", "EPS current", "Volume", "RSI",
                "Status", "Sector", "Drawdown %", "Above 52W Low %", "Higher Low", "Tech Score", "Stop Loss", "Target 1", "Target 2",
                "Risk Reward", "Notes", "Updated"]


class TestManifest(unittest.TestCase):
    def test_golden_files_unchanged(self):
        m = _j(os.path.join(ROOT, "golden_master", "MANIFEST.json"))
        for name, meta in m["files"].items():
            with open(os.path.join(GM, name), "rb") as fh:
                h = hashlib.sha256(fh.read()).hexdigest()
            self.assertEqual(h, meta["sha256"], f"{name} changed - golden masters are read-only")


class TestTechnical(unittest.TestCase):
    def setUp(self): self.d = load("technical_trading_alerts.json")

    def test_schema_and_count(self):
        self.assertEqual(len(self.d), 217)
        for r in self.d:
            self.assertEqual(list(r), ["ticker", "action", "entry", "sl", "tp1", "tp2", "tp3", "aligned", "plan", "time_received"])

    def test_risk_multiples_1_2_3(self):
        for r in self.d:
            risk = r["entry"] - r["sl"]
            self.assertGreater(risk, 0)
            for k, m in (("tp1", 1), ("tp2", 2), ("tp3", 3)):
                self.assertAlmostEqual(r[k] - r["entry"], risk * m, delta=0.03, msg=r["ticker"])

    def test_unique_tickers(self):
        self.assertEqual(len({r["ticker"] for r in self.d}), len(self.d))

    def test_dashboard_baseline(self):
        b = _j(os.path.join(ROOT, "golden_master/characterization/technical_dashboard_filter_baseline.json"))
        self.assertEqual(b["rows_after_hidden_filters"], 10)


class TestFundamental(unittest.TestCase):
    def setUp(self): self.d = load("fundamental_data.json")

    def test_schema_count_unique(self):
        self.assertEqual(len(self.d), 344)
        self.assertTrue(all(list(r) == FUND_COLUMNS for r in self.d))
        self.assertEqual(len({r["Ticker"] for r in self.d}), 344)

    def test_sort_order(self):
        keys = [(STATUS_ORDER[r["Status"]], -r["Tech Score"]) for r in self.d]
        self.assertEqual(keys, sorted(keys))

    def test_fund_thresholds_hold_for_non_fund_wait(self):
        """Stored rounded values must satisfy MCap>1000, Sales>8, OPM>8, ROE>10, ROCE>10 for every status except WAIT (Fund)."""
        bad = [r["Ticker"] for r in self.d if r["Status"] != "WAIT (Fund)" and not (
            r["Market Cap"] > 1000 and r["Sales growth"] > 8 and r["OPM"] > 8 and r["ROE"] > 10 and r["ROCE"] > 10)]
        self.assertEqual(bad, [])

    def test_valuation_and_zone_per_status(self):
        for r in self.d:
            s = r["Status"]
            if s in ("🔥 PASS", "👀 WATCH", "WAIT (Price)"):
                self.assertTrue(0 < r["PE"] <= 35, r["Ticker"])
            if s == "👀 WATCH":
                self.assertGreaterEqual(r["Drawdown %"], 25, r["Ticker"])
            if s == "WAIT (Price)":
                self.assertLess(r["Drawdown %"], 25, r["Ticker"])
            if s == "🔥 PASS":
                self.assertTrue(35 <= r["Drawdown %"] <= 65 and r["Tech Score"] >= 4 and r["Risk Reward"] >= 1.5)

    def test_wait_fund_really_fails_a_check_when_visible(self):
        """For WAIT (Fund) the Notes column must list at least one 'Fund:' reason."""
        for r in self.d:
            if r["Status"] == "WAIT (Fund)":
                self.assertIn("Fund:", r["Notes"], r["Ticker"])


def _pct(s):
    m = re.match(r"\s*(-?[\d.]+)%", s)
    return float(m.group(1)) if m else None


class TestShariah(unittest.TestCase):
    def setUp(self):
        p = load("shariah_latest_results.json"); self.d = p["data"]

    def test_count_unique(self):
        self.assertEqual(len(self.d), 1228)
        self.assertEqual(len({r["ticker"] for r in self.d}), 1228)

    def test_verdict_is_and_of_six(self):
        bad = []
        for r in self.d:
            st = [c["status"] for c in r["metrics"].values()]
            if len(st) == 6 and r["is_compliant"] != all(st):
                bad.append(r["ticker"])
        self.assertEqual(bad, [])

    def test_status_matches_metric_vs_threshold(self):
        """Percent metrics must agree with their thresholds (C1,C2 <5; C3,C6 <33; C4 >=20)."""
        rules = {"Criteria 1: Core Business": lambda x: x < 5, "Criteria 2: Impermissible Income": lambda x: x < 5,
                 "Criteria 3: Debt / Assets": lambda x: x < 33, "Criteria 4: Illiquid / Assets": lambda x: x >= 20,
                 "Criteria 6: Non-Compliant Inv": lambda x: x < 33}
        bad = []
        for r in self.d:
            for k, fn in rules.items():
                c = r["metrics"].get(k)
                if c and _pct(str(c.get("metric", ""))) is not None and fn(_pct(c["metric"])) != c["status"]:
                    bad.append((r["ticker"], k, c["metric"]))
        self.assertEqual(bad, [])

    def test_c5_metric_consistent(self):
        bad = []
        for r in self.d:
            c = r["metrics"].get("Criteria 5: Net Liquid < MCap")
            m = re.search(r"Net Liq: ₹(-?[\d,]+) Cr \| MCap: ₹([\d,]+) Cr", (c or {}).get("metric", ""))
            if m:
                nl, mc = (float(x.replace(",", "")) for x in m.groups())
                if (nl < mc) != c["status"] and abs(nl - mc) > 1:      # ±1 Cr display rounding tolerated
                    bad.append(r["ticker"])
        self.assertEqual(bad, [])

    def test_known_gaps_are_visible(self):
        """Documents the data-quality gaps the strict-6-criteria rule will remove."""
        self.assertGreater(sum(1 for r in self.d if r["bse_code"] == "N/A"), 1200)
        self.assertTrue(all(r["metrics"]["Criteria 1: Core Business"]["metric"] in ("0.00%", "100.0%", "N/A")
                            or "Criteria 1: Core Business" not in r["metrics"] for r in self.d if "Criteria 1: Core Business" in r["metrics"]))


if __name__ == "__main__":
    unittest.main()
