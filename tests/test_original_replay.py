"""Replays the saved characterization fixtures through the ORIGINAL code and asserts identical results.
Purpose: (1) proves the fixtures are deterministic; (2) the same fixtures become the parity target for the
refactored engines in later phases (swap `original_*` callables for the new engine functions)."""
import json, math, os, sys, unittest
sys.path.insert(0, os.path.dirname(__file__))
import harness
import pandas as pd

CH = os.path.join(harness.ROOT, "golden_master", "characterization")


def J(n):
    with open(os.path.join(CH, n), encoding="utf-8") as fh:
        return json.load(fh)


def norm(o):
    """JSON round-trip with NaN -> 'NaN' so comparisons are stable."""
    return json.loads(json.dumps(o, default=lambda x: "NaN" if (isinstance(x, float) and math.isnan(x)) else str(x)))


class TestShariahReplay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.const, cls.engine, cls.xp = harness.load_shariah()

    def test_engine_cases(self):
        for c in J("shariah_engine_cases.json"):
            got = self.engine.evaluate_all_criteria(c["market_cap"], c["financials"], c["audit"])
            self.assertEqual(norm(got), norm(c["expected"]), c["name"])

    def test_parser_xml_cases(self):
        import tempfile
        from fixture_builders import xbrl
        fx = J("shariah_xbrl_parser_cases.json")
        for name, c in fx.items():
            if "tags" not in c:
                continue
            p = os.path.join(tempfile.mkdtemp(), name + ".xml")
            with open(p, "w") as fh:
                fh.write(xbrl([tuple(t) for t in c["tags"]]))
            self.assertEqual(norm(self.xp.parse_xbrl_primitives(p)), norm(c["expected"]), name)


class TestFundamentalReplay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fw = harness.load_fundamental()

    @staticmethod
    def hist(h):
        df = pd.DataFrame({k: h[k] for k in ["Open", "High", "Low", "Close", "Volume"]}, index=pd.to_datetime(h["dates"]))
        return df

    def test_technicals(self):
        for c in J("fundamental_technicals_cases.json")["cases"]:
            got = self.fw.technicals(self.hist(c["hist"]))
            got = {k: (bool(v) if isinstance(v, (bool,)) or str(type(v)).endswith("bool'>") or str(type(v)).endswith("bool_'>") else float(v)) for k, v in got.items()}
            for k, v in c["expected"].items():
                self.assertAlmostEqual(float(got[k]), float(v), places=9, msg=f"{c['name']}.{k}")

    def test_scan_ticker_rows(self):
        from fixture_builders import fin
        for sc in J("fundamental_scan_ticker_cases.json")["scenarios"]:
            ib = sc["income_balance"]
            inc, bal = fin() if ib == "default" else ((None, None) if ib is None else fin(**ib))
            sym = sc["name"] + ".NS"
            harness.FakeTicker.registry[sym] = harness.FakeTicker(sym, info=sc["info"], hist=self.hist(sc["hist"]), income=inc, balance=bal)
            row = self.fw.scan_ticker(sym)
            if row:
                row.pop("Updated")
            self.assertEqual(norm(row), norm(sc["expected_row"]), sc["name"])

    def test_all_five_statuses_covered(self):
        got = {s["expected_row"]["Status"] for s in J("fundamental_scan_ticker_cases.json")["scenarios"] if s["expected_row"]}
        self.assertEqual(got, {"🔥 PASS", "👀 WATCH", "WAIT (Price)", "WAIT (Valuation)", "WAIT (Fund)"})


if __name__ == "__main__":
    unittest.main()
