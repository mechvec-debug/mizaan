"""Phase 3 tests - Universe Service. Run: python -m unittest discover -s tests -v"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mizan.core.errors import EmptyUniverseError, UniverseValidationError  # noqa: E402
from mizan.universe import UniverseService, invalid_reason, normalize_symbol, to_yahoo  # noqa: E402
from mizan.universe.parser import parse_bulk, parse_text  # noqa: E402

REAL = ROOT / "data" / "universe" / "stock_universe.txt"
GOLD = ROOT / "golden_master" / "original_outputs"


def jload(name):
    with open(GOLD / name, encoding="utf-8") as fh:
        return json.load(fh)


class TmpUniverse(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.file = self.dir / "stock_universe.txt"

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def svc(self, text, **kw):
        self.file.write_text(text, encoding="utf-8")
        return UniverseService(self.file, **kw)


class TestNormalize(unittest.TestCase):
    def test_table(self):
        cases = {"tcs": "TCS", " TCS ": "TCS", "TCS.NS": "TCS", "tcs.bo": "TCS", "NSE:INFY": "INFY", "bse: wipro .NS": "WIPRO",
                 '"M&M"': "M&M", "bajaj-auto": "BAJAJ-AUTO", "ARE&M": "ARE&M"}
        for raw, want in cases.items():
            self.assertEqual(normalize_symbol(raw), want, raw)

    def test_validity(self):
        for ok in ["TCS", "M&M", "BAJAJ-AUTO", "ARE&M", "3BBLACKBIO", "20MICRONS", "BOSCH-HCIL"]:
            self.assertIsNone(invalid_reason(ok), ok)
        for bad in ["", "TA TA", "-TCS", "&X", "TCS/NS", "A" * 21, "TC$", "TCS.XX"]:
            self.assertIsNotNone(invalid_reason(bad), bad)

    def test_yahoo_form_matches_original(self):
        self.assertEqual(to_yahoo("TCS"), "TCS.NS")
        self.assertEqual(to_yahoo("M&M"), "M&M.NS")


class TestParse(unittest.TestCase):
    def test_comments_blank_dupes_invalid_disabled(self):
        t = "# header comment with many words\n\nTCS\ninfy  # Infosys\n# WIPRO\nTCS\nBAD SYMBOL\nHDFC$\n"
        by = {}
        for e in parse_text(t):
            by.setdefault(e.status, []).append(e)
        self.assertEqual([e.symbol for e in by["active"]], ["TCS", "INFY"])
        self.assertEqual(by["active"][1].note, "Infosys")
        self.assertEqual([e.symbol for e in by["disabled"]], ["WIPRO"])
        self.assertEqual(len(by["duplicate"]), 1)
        self.assertEqual(len(by["invalid"]), 2)

    def test_original_identifier_preserved(self):
        e = parse_text("tcs.ns")[0]
        self.assertEqual((e.raw, e.symbol), ("tcs.ns", "TCS"))

    def test_bom_and_crlf(self):
        syms = [e.symbol for e in parse_text("\ufeffTCS\r\nINFY\r\n")]
        self.assertEqual(syms, ["TCS", "INFY"])

    def test_bulk_formats(self):
        self.assertEqual(parse_bulk("TCS\nINFY\n# c\n"), ["TCS", "INFY"])
        self.assertEqual(parse_bulk("TCS, INFY, WIPRO"), ["TCS", "INFY", "WIPRO"])
        self.assertEqual(parse_bulk("Company,Symbol,Sector\nTata,TCS,IT\nInfy,INFY,IT\n"), ["TCS", "INFY"])
        self.assertEqual(parse_bulk("Tata Motors\n"), ["Tata Motors"])          # kept whole -> rejected later

    def test_bulk_ambiguous_csv_is_refused(self):
        with self.assertRaises(ValueError):
            parse_bulk("Company,Sector\nTata,IT\nInfosys,IT\n")


class TestService(TmpUniverse):
    def test_loads_and_orders(self):
        s = self.svc("TCS\nINFY\n").get()
        self.assertEqual(s.tickers, ("TCS", "INFY"))
        self.assertEqual(s.count, 2)

    def test_empty_universe_refuses(self):
        with self.assertRaises(EmptyUniverseError):
            self.svc("# nothing here\n\n").get()

    def test_missing_file(self):
        with self.assertRaises(Exception):
            UniverseService(self.dir / "nope.txt").get()

    def test_invalid_excluded_but_reported(self):
        s = self.svc("TCS\nBAD SYM\n").get()
        self.assertEqual(s.tickers, ("TCS",))
        self.assertEqual(len(s.invalid), 1)
        self.assertFalse(s.ok)

    def test_strict_mode_raises(self):
        with self.assertRaises(UniverseValidationError):
            self.svc("TCS\nBAD SYM\n", strict=True).get()

    def test_version_stable_under_order_case_comments(self):
        a = self.svc("TCS\nINFY\n").get().version
        b = self.svc("# note\ninfy\ntcs.ns\n").get().version
        self.assertEqual(a, b)
        self.assertNotEqual(a, self.svc("TCS\nINFY\nWIPRO\n").get().version)

    def test_auto_reload_when_file_changes(self):
        svc = self.svc("TCS\n")
        v1 = svc.get().version
        time.sleep(0.02)
        self.file.write_text("TCS\nINFY\n", encoding="utf-8")
        s2 = svc.get()
        self.assertEqual(s2.tickers, ("TCS", "INFY"))
        self.assertNotEqual(v1, s2.version)

    def test_snapshot_is_immutable(self):
        s = self.svc("TCS\n").get()
        with self.assertRaises(Exception):
            s.tickers = ("X",)
        self.assertIsInstance(s.tickers, tuple)

    def test_subset_preserves_order(self):
        s = self.svc("TCS\nINFY\nWIPRO\n").get()
        self.assertEqual(s.subset({"WIPRO", "TCS", "ZZZ"}), ("TCS", "WIPRO"))

    def test_manifest(self):
        svc = self.svc("TCS\nINFY\n")
        out = svc.write_manifest(self.dir / "m" / "universe.json")
        d = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual((d["active"], d["tickers"]), (2, ["TCS", "INFY"]))


class TestEditing(TmpUniverse):
    HDR = "# keep me\n# TO BE REMOVED NOTE\nTCS\n"

    def test_bulk_add_reports_everything(self):
        svc = self.svc(self.HDR + "# WIPRO\n")
        r = svc.add(["infy", "TCS", "wipro", "BAD SYM", "infy.ns", "M&M"])
        self.assertEqual(r.added, ["INFY", "M&M"])
        self.assertEqual(r.already_present, ["TCS", "INFY"])
        self.assertEqual(r.exists_but_disabled, ["WIPRO"])
        self.assertEqual([x[0] for x in r.invalid], ["BAD SYM"])
        self.assertEqual(svc.get().tickers, ("TCS", "INFY", "M&M"))
        self.assertIn("# keep me", self.file.read_text(encoding="utf-8"))     # comments survive

    def test_add_from_text_csv(self):
        svc = self.svc("TCS\n")
        r = svc.add_from_text("Name,Ticker\nInfosys,INFY\nWipro,WIPRO\n")
        self.assertEqual(r.added, ["INFY", "WIPRO"])

    def test_remove_disable_enable_roundtrip(self):
        svc = self.svc("TCS\nINFY\nWIPRO\n")
        self.assertEqual(svc.disable(["infy"]).disabled, ["INFY"])
        self.assertEqual(svc.get().tickers, ("TCS", "WIPRO"))
        self.assertEqual(svc.enable(["INFY"]).enabled, ["INFY"])
        self.assertEqual(svc.get().tickers, ("TCS", "INFY", "WIPRO"))
        r = svc.remove(["WIPRO", "NOPE"])
        self.assertEqual((r.removed, r.not_found), (["WIPRO"], ["NOPE"]))
        self.assertEqual(svc.get().tickers, ("TCS", "INFY"))

    def test_backup_created_and_pruned(self):
        svc = self.svc("TCS\n")
        for i in range(25):
            svc.add([f"S{i}"])
        self.assertLessEqual(len(list((self.dir / "backups").glob("*.txt"))), 20)

    def test_edit_noop_does_not_touch_file(self):
        svc = self.svc("TCS\n")
        before = self.file.read_text(encoding="utf-8")
        r = svc.add(["TCS"])
        self.assertFalse(r.changed)
        self.assertEqual(self.file.read_text(encoding="utf-8"), before)
        self.assertFalse((self.dir / "backups").exists())

    def test_version_changes_after_add(self):
        svc = self.svc("TCS\n")
        v = svc.get().version
        r = svc.add(["INFY"])
        self.assertNotEqual(v, r.new_version)


class TestRealUniverse(unittest.TestCase):
    """The committed provisional file against the golden masters."""

    @classmethod
    def setUpClass(cls):
        cls.snap = UniverseService(REAL).get(strict=True)       # strict: real file must be 100% clean

    def test_clean_and_complete(self):
        self.assertEqual(self.snap.count, 1228)
        self.assertEqual((len(self.snap.invalid), len(self.snap.duplicates)), (0, 0))

    def test_covers_every_golden_shariah_ticker(self):
        gold = {r["ticker"] for r in jload("shariah_latest_results.json")["data"]}
        self.assertEqual(set(self.snap.tickers), gold)

    def test_special_symbols_survive(self):
        for s in ("ARE&M", "BOSCH-HCIL", "GMRP&UI", "KLBRENG-B", "MCCHRLS-B"):
            self.assertIn(s, self.snap.tickers)

    def test_every_symbol_is_already_normalised(self):
        self.assertTrue(all(normalize_symbol(t) == t for t in self.snap.tickers))

    def test_default_config_path_resolves_to_same_file(self):
        self.assertEqual(UniverseService().get().version, self.snap.version)



class TestCandidatesFile(unittest.TestCase):
    def test_candidates_valid_and_disjoint_from_universe(self):
        text = (ROOT / "data" / "universe" / "candidates_from_other_screeners.txt").read_text(encoding="utf-8")
        syms = parse_bulk(text)
        self.assertEqual(len(syms), 82)
        self.assertTrue(all(invalid_reason(normalize_symbol(s)) is None for s in syms))
        uni = set(UniverseService(REAL).get().tickers)
        self.assertEqual(uni & {normalize_symbol(s) for s in syms}, set())


if __name__ == "__main__":
    unittest.main()
