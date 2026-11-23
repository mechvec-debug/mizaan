"""Phase 7 tests - Shariah Compliance Screener (strict six criteria). Run: python -m unittest discover -s tests -v"""
import csv
import io
import json
import math
import re
import shutil
import sys
import tempfile
import unittest
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from fakes import FakeClock  # noqa: E402
from mizan.core.errors import PermanentProviderError, RateLimitError  # noqa: E402
from mizan.data import DataManager  # noqa: E402
from mizan.data.providers.bse import BSE_API_URL, BseXbrlProvider  # noqa: E402
from mizan.screeners.shariah import (CRITERIA, COMPLIANT, NON_COMPLIANT, UNDETERMINED, BseMapper, EvidenceStore, ShariahParams,  # noqa: E402
                                     build_map, evaluate, evaluate_stock, parse_xbrl_facts, run, scan_universe, write_map, write_outputs)
from mizan.screeners.shariah.bundles import bundle_from_xbrl, bundle_from_yahoo  # noqa: E402
from mizan.universe import UniverseService  # noqa: E402

CH = ROOT / "golden_master" / "characterization"
P = ShariahParams()
FAST = {"throttle": {"requests_per_minute": 0, "max_retries": 0, "breaker_threshold": 3}}
NOW = datetime(2026, 11, 3, 10, 0, tzinfo=timezone.utc)


# ============================================================ builders
def xbrl_xml(ctx, facts, scale=None):
    parts = ['<?xml version="1.0"?><xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" '
             'xmlns:xbrldi="http://xbrl.org/2006/xbrldi" xmlns:in-capmkt="http://example.com/in">']
    for cid, c in ctx.items():
        seg = '<xbrli:segment><xbrldi:explicitMember dimension="d">m</xbrldi:explicitMember></xbrli:segment>' if c.get("dim") else ""
        per = (f"<xbrli:instant>{c['instant']}</xbrli:instant>" if "instant" in c
               else f"<xbrli:startDate>{c['start']}</xbrli:startDate><xbrli:endDate>{c['end']}</xbrli:endDate>")
        parts.append(f'<xbrli:context id="{cid}"><xbrli:entity><xbrli:identifier scheme="x">A</xbrli:identifier>{seg}</xbrli:entity>'
                     f"<xbrli:period>{per}</xbrli:period></xbrli:context>")
    for tag, cid, v in facts:
        parts.append(f'<in-capmkt:{tag} contextRef="{cid}" unitRef="INR" decimals="0">{v}</in-capmkt:{tag}>')
    if scale:
        parts.append(f'<in-capmkt:LevelOfRoundingUsedInFinancialStatements contextRef="I1">{scale}</in-capmkt:LevelOfRoundingUsedInFinancialStatements>')
    return "".join(parts + ["</xbrli:xbrl>"]).encode()


CTX = {"I1": {"instant": "2026-03-31"}, "I0": {"instant": "2025-03-31"}, "DIM": {"instant": "2026-03-31", "dim": True},
       "D1": {"start": "2025-04-01", "end": "2026-03-31"}, "D0": {"start": "2024-04-01", "end": "2025-03-31"},
       "Q": {"start": "2026-01-01", "end": "2026-03-31"}}
FACTS = [("Assets", "I1", 1000), ("Assets", "I0", 900), ("Assets", "DIM", 300), ("Liabilities", "I1", 400),
         ("EquityAndLiabilities", "I1", 1000), ("RevenueFromOperations", "D1", 800), ("RevenueFromOperations", "D0", 700),
         ("RevenueFromOperations", "Q", 200), ("InterestIncome", "D1", 12), ("BorrowingsCurrent", "I1", 20),
         ("BorrowingsNonCurrent", "I1", 80), ("PropertyPlantAndEquipment", "I1", 300), ("Inventories", "I1", 100)]


def ystm(bs, pnl, fy="2026-03-31", prior="2025-03-31", pnl_fy=None):
    """Yahoo-style statements; inputs in Rs crore, stored in rupees (as Yahoo does)."""
    cols = [pd.Timestamp(fy), pd.Timestamp(prior)]
    mk = lambda rows, c0: pd.DataFrame({c0: [v * 1e7 for v in rows.values()], cols[1]: [np.nan] * len(rows)}, index=list(rows), columns=[c0, cols[1]])
    return {"balance_sheet": mk(bs, cols[0]), "income_stmt": mk(pnl, pd.Timestamp(pnl_fy or fy)), "financials": None}


GOOD_BS = {"Total Assets": 1000, "Total Liabilities Net Minority Interest": 400, "Total Debt": 100, "Net PPE": 300, "Inventory": 100,
           "Cash Cash Equivalents And Short Term Investments": 150, "Receivables": 100, "Current Assets": 400}
GOOD_PNL = {"Total Revenue": 800, "Interest Income": 8}
BASE = dict(total_assets=1000.0, total_revenue=1000.0, total_liabilities=500.0, debt=100.0, debt_lb=100.0, illiquid_lb=400.0,
            illiquid_exact=400.0, liquid_lo=300.0, liquid_hi=300.0, liquid_exact=300.0, interest_income=10.0, inv_ub=None)
EV_OK = {"haram_business_revenue": 0.0, "non_compliant_investments": 50.0}


def ev(p=None, mcap=5000.0, evidence=None, to_crore=1.0, **kw):
    return evaluate({**BASE, **(p or {}), **kw}, to_crore, mcap, EV_OK if evidence is None else evidence, P)


def norm(o):
    return json.loads(json.dumps(o, default=str))


# ============================================================ 1) engine parity with the original, boundary by boundary
class TestEngineParityWithOriginal(unittest.TestCase):
    def test_every_decidable_original_case_gives_the_same_statuses_and_metrics(self):
        cases = json.loads((CH / "shariah_engine_cases.json").read_text(encoding="utf-8"))
        n = 0
        for c in cases:
            if c["name"].startswith("guard_"):
                continue                                              # deliberately different (see next test)
            f, a = c["financials"], c["audit"]
            p = dict(total_assets=f["total_assets"], total_revenue=f["total_revenue"], total_liabilities=f["total_liabilities"],
                     debt=f["interest_bearing_debt"], debt_lb=f["interest_bearing_debt"], illiquid_lb=f["illiquid_assets"],
                     illiquid_exact=f["illiquid_assets"], liquid_lo=f["liquid_assets"], liquid_hi=f["liquid_assets"],
                     liquid_exact=f["liquid_assets"], interest_income=f["impermissible_interest_income"], inv_ub=None)
            evid = {"haram_business_revenue": a["haram_business_revenue"], "impermissible_interest_income": a["impermissible_interest_income"],
                    "non_compliant_investments": a["non_compliant_investments"] or f["non_compliant_investments"]}
            verdict, out = evaluate(p, 1.0, c["market_cap"], evid, P)
            want = c["expected"]
            self.assertEqual(verdict == COMPLIANT, want["is_compliant"], c["name"])
            for name, w in want["results"].items():
                self.assertEqual(out[name].status, w["status"], f"{c['name']} / {name}")
                self.assertEqual(out[name].metric, w["metric"], f"{c['name']} / {name}")
                self.assertEqual(out[name].threshold, w["threshold"], f"{c['name']} / {name}")
            n += 1
        self.assertGreaterEqual(n, 15)

    def test_guard_cases_are_undetermined_not_false(self):
        """Original: zero/NaN assets or revenue => 'Incomplete Statements' (all criteria False). Now: only what cannot be computed is None."""
        by = {c["name"]: c for c in json.loads((CH / "shariah_engine_cases.json").read_text(encoding="utf-8"))}
        for name, none_criteria in (("guard_zero_assets", {2, 3, 5}), ("guard_nan_assets", {2, 3, 5}), ("guard_zero_revenue", {0, 1})):
            f, a = by[name]["financials"], by[name]["audit"]
            p = dict(BASE, total_assets=f["total_assets"], total_revenue=f["total_revenue"])
            verdict, out = evaluate(p, 1.0, 5000.0, {"haram_business_revenue": 0.0, "impermissible_interest_income": 10.0, "non_compliant_investments": 50.0}, P)
            self.assertEqual(verdict, UNDETERMINED, name)
            self.assertEqual({i for i, n in enumerate(CRITERIA) if out[n].status is None}, none_criteria, name)


# ============================================================ 2) strict policy
class TestStrictPolicy(unittest.TestCase):
    def test_c1_unknown_means_never_compliant(self):
        v, out = ev(evidence={})
        self.assertEqual(v, UNDETERMINED)
        self.assertIsNone(out[CRITERIA[0]].status)
        self.assertIn("pending", out[CRITERIA[0]].note)

    def test_complete_evidence_gives_compliant(self):
        v, out = ev()
        self.assertEqual(v, COMPLIANT)
        self.assertTrue(all(o.status is True for o in out.values()))

    def test_a_breach_is_decisive_even_with_other_data_missing(self):
        v, out = ev(evidence={}, debt=400.0, debt_lb=400.0)
        self.assertEqual(v, NON_COMPLIANT)
        self.assertIs(out[CRITERIA[2]].status, False)
        self.assertIsNone(out[CRITERIA[0]].status)

    def test_conflicting_interest_figures_use_the_larger(self):
        v, out = ev(interest_income=60.0, evidence={"haram_business_revenue": 0.0, "impermissible_interest_income": 10.0, "non_compliant_investments": 0.0})
        self.assertIs(out[CRITERIA[1]].status, False)                  # statements say 6 %, evidence says 1 % -> 6 % rules
        self.assertTrue(out[CRITERIA[1]].metric.startswith("6.00%"))

    def test_c6_upper_bound(self):
        v, out = ev(evidence={"haram_business_revenue": 0.0}, inv_ub=200.0)
        self.assertIs(out[CRITERIA[5]].status, True)
        self.assertIn("upper bound", out[CRITERIA[5]].metric)
        v, out = ev(evidence={"haram_business_revenue": 0.0}, inv_ub=400.0)               # bound too high: cannot decide
        self.assertIsNone(out[CRITERIA[5]].status)
        v, out = ev(evidence={"haram_business_revenue": 0.0}, inv_ub=None)
        self.assertIsNone(out[CRITERIA[5]].status)

    def test_c5_bounds(self):
        v, out = ev(liquid_exact=None, liquid_lo=100.0, liquid_hi=1000.0, mcap=5000.0)      # even the upper bound < mcap -> pass
        self.assertIs(out[CRITERIA[4]].status, True)
        v, out = ev(liquid_exact=None, liquid_lo=6000.0, liquid_hi=9000.0, mcap=5000.0)     # even the lower bound >= mcap -> breach
        self.assertIs(out[CRITERIA[4]].status, False)
        v, out = ev(liquid_exact=None, liquid_lo=100.0, liquid_hi=9000.0, mcap=5000.0)      # in between -> undetermined
        self.assertIsNone(out[CRITERIA[4]].status)
        v, out = ev(mcap=None)
        self.assertIsNone(out[CRITERIA[4]].status)

    def test_c4_and_c3_bounds(self):
        _, out = ev(illiquid_exact=None, illiquid_lb=250.0)
        self.assertIs(out[CRITERIA[3]].status, True)
        _, out = ev(illiquid_exact=None, illiquid_lb=100.0)
        self.assertIsNone(out[CRITERIA[3]].status)
        _, out = ev(debt=None, debt_lb=400.0)
        self.assertIs(out[CRITERIA[2]].status, False)
        _, out = ev(debt=None, debt_lb=100.0)
        self.assertIsNone(out[CRITERIA[2]].status)

    def test_unknown_scale_only_blocks_what_needs_it(self):
        _, out = ev(to_crore=None)
        self.assertIsNone(out[CRITERIA[0]].status)                      # rupee evidence cannot be turned into a ratio
        self.assertIsNone(out[CRITERIA[4]].status)                      # cannot compare with market cap
        self.assertIs(out[CRITERIA[1]].status, True)                    # ratios inside one filing are scale-free
        self.assertIs(out[CRITERIA[2]].status, True)
        self.assertNotIn("₹", out[CRITERIA[1]].metric)

    def test_thresholds_are_the_original_ones(self):
        self.assertEqual((P.max_haram_business_revenue_pct, P.max_impermissible_income_pct, P.max_debt_to_assets_pct,
                          P.min_illiquid_assets_pct, P.max_non_compliant_inv_pct), (5.0, 5.0, 33.0, 20.0, 33.0))

    def test_bank_judged_on_the_criteria_not_by_sector(self):
        """Interest business is impermissible income: a bank fails Criterion 2 from its own numbers; no sector shortcut."""
        bs = {"Total Assets": 100000, "Total Liabilities Net Minority Interest": 92000}
        b = bundle_from_yahoo(ystm(bs, {"Total Revenue": 10000, "Interest Income": 7000}))
        v, out = evaluate(b["p"], b["to_crore"], 80000.0, {}, P)
        self.assertEqual(v, NON_COMPLIANT)
        self.assertIs(out[CRITERIA[1]].status, False)
        self.assertEqual(out[CRITERIA[1]].metric, "70.00% (₹7,000 Cr)")
        self.assertIsNone(out[CRITERIA[0]].status)                      # core business still needs real evidence

    def test_no_stock_specific_override(self):
        import inspect
        import mizan.screeners.shariah.scanner as sc
        import mizan.screeners.shariah.criteria as cr
        for mod in (sc, cr):
            self.assertNotIn("ITC", inspect.getsource(mod))


# ============================================================ 3) XBRL parser
class TestXbrlParser(unittest.TestCase):
    def test_picks_current_year_non_dimensional_values(self):
        x = parse_xbrl_facts(xbrl_xml(CTX, FACTS))
        f = {k: v["value"] for k, v in x["facts"].items()}
        self.assertEqual((x["fy_end"], x["period_days"], x["annual"]), ("2026-03-31", 364, True))
        self.assertEqual((f["total_assets"], f["total_revenue"], f["interest_income"]), (1000, 800, 12))   # not 900 / 700 / 200 / 300

    def test_total_liabilities_uses_liabilities_not_equity_and_liabilities(self):
        x = parse_xbrl_facts(xbrl_xml(CTX, FACTS))
        self.assertEqual((x["facts"]["total_liabilities"]["value"], x["facts"]["total_liabilities"]["tag"]), (400, "Liabilities"))

    def test_liabilities_identity_when_tag_missing(self):
        facts = [f for f in FACTS if f[0] != "Liabilities"] + [("Equity", "I1", 600)]
        x = parse_xbrl_facts(xbrl_xml(CTX, facts))
        self.assertEqual(x["facts"]["total_liabilities"], {"value": 400, "tag": "EquityAndLiabilities-Equity"})
        x2 = parse_xbrl_facts(xbrl_xml(CTX, [f for f in FACTS if f[0] != "Liabilities"]))
        self.assertNotIn("total_liabilities", x2["facts"])                # never fall back to EquityAndLiabilities itself

    def test_scale_read_from_filing_never_guessed(self):
        x = parse_xbrl_facts(xbrl_xml(CTX, FACTS, scale="Lakhs"))
        self.assertEqual(x["scale"], {"text": "lakhs", "to_rupees": 1e5})
        self.assertAlmostEqual(bundle_from_xbrl(x)["to_crore"], 0.01)
        big = parse_xbrl_facts(xbrl_xml(CTX, [("Assets", "I1", 20000000), ("RevenueFromOperations", "D1", 10000000)]))
        self.assertIsNone(big["scale"])                                   # >1e7 values are NOT silently divided
        self.assertEqual(big["facts"]["total_assets"]["value"], 20000000)
        self.assertIsNone(bundle_from_xbrl(big)["to_crore"])

    def test_quarterly_filing_is_not_annual(self):
        facts = [("Assets", "I1", 1000), ("RevenueFromOperations", "Q", 200)]
        x = parse_xbrl_facts(xbrl_xml(CTX, facts))
        self.assertEqual((x["period_days"], x["annual"]), (89, False))
        self.assertIsNone(bundle_from_xbrl(x))

    def test_interest_total_beats_parts_and_parts_are_summed_otherwise(self):
        parts = [f for f in FACTS if f[0] != "InterestIncome"] + [("InterestIncomeFromBankDeposits", "D1", 5), ("InterestIncomeOthers", "D1", 3)]
        self.assertEqual(parse_xbrl_facts(xbrl_xml(CTX, parts))["facts"]["interest_income"]["value"], 8)
        bank = [f for f in FACTS if f[0] != "InterestIncome"] + [("InterestEarned", "D1", 700)]
        self.assertEqual(parse_xbrl_facts(xbrl_xml(CTX, bank))["facts"]["interest_income"], {"value": 700, "tag": "InterestEarned"})

    def test_empty_and_path_input(self):
        x = parse_xbrl_facts(xbrl_xml(CTX, []))
        self.assertEqual((x["fy_end"], x["annual"]), (None, False))
        d = Path(tempfile.mkdtemp())
        try:
            (d / "f.xml").write_bytes(xbrl_xml(CTX, FACTS))
            self.assertEqual(parse_xbrl_facts(d / "f.xml")["fy_end"], "2026-03-31")
        finally:
            shutil.rmtree(d, ignore_errors=True)


# ============================================================ 4) bundles (source -> primitives)
class TestBundles(unittest.TestCase):
    def test_yahoo_bundle_basics(self):
        b = bundle_from_yahoo(ystm(GOOD_BS, GOOD_PNL))
        p = b["p"]
        self.assertEqual((b["source"], b["fy_end"], b["to_crore"]), ("yahoo", "2026-03-31", 1.0))
        self.assertEqual((p["total_assets"], p["total_revenue"], p["debt"], p["interest_income"]), (1000, 800, 100, 8))
        self.assertEqual((p["illiquid_lb"], p["illiquid_exact"]), (400, None))          # PPE+inventory known; intangibles/property unknown
        self.assertEqual((p["liquid_lo"], p["liquid_hi"], p["liquid_exact"]), (250, 400, None))

    def test_revenue_from_another_fiscal_year_is_not_mixed_in(self):
        b = bundle_from_yahoo(ystm(GOOD_BS, GOOD_PNL, pnl_fy="2025-03-31"))     # income statement only has the PRIOR year column
        self.assertIsNone(b["p"]["total_revenue"])

    def test_intangibles_not_double_counted_and_leases_ignored(self):
        bs = dict(GOOD_BS, **{"Goodwill And Other Intangible Assets": 100, "Goodwill": 60, "Other Intangible Assets": 40, "Leases": 999})
        b = bundle_from_yahoo(ystm(bs, GOOD_PNL))
        self.assertEqual(b["p"]["illiquid_lb"], 300 + 100 + 100)                           # PPE + inventory + aggregate intangibles
        bs2 = dict(GOOD_BS, **{"Goodwill": 60, "Other Intangible Assets": 40})
        self.assertEqual(bundle_from_yahoo(ystm(bs2, GOOD_PNL))["p"]["illiquid_lb"], 300 + 100 + 100)   # components summed when no aggregate

    def test_total_liabilities_identity_and_no_wrong_fallback(self):
        bs = {k: v for k, v in GOOD_BS.items() if k != "Total Liabilities Net Minority Interest"}
        bs["Total Equity Gross Minority Interest"] = 600
        self.assertEqual(bundle_from_yahoo(ystm(bs, GOOD_PNL))["p"]["total_liabilities"], 400)
        bs2 = {k: v for k, v in GOOD_BS.items() if k != "Total Liabilities Net Minority Interest"}
        bs2["Total Non Current Liabilities Net Minority Interest"] = 50                     # the original wrongly used this
        self.assertIsNone(bundle_from_yahoo(ystm(bs2, GOOD_PNL))["p"]["total_liabilities"])

    def test_net_interest_row_is_not_gross_interest_income(self):
        b = bundle_from_yahoo(ystm(GOOD_BS, {"Total Revenue": 800, "Net Non Operating Interest Income Expense": 30}))
        self.assertIsNone(b["p"]["interest_income"])

    def test_missing_rows_are_unknown_not_zero(self):
        b = bundle_from_yahoo(ystm({"Total Assets": 1000}, {"Total Revenue": 800}))
        p = b["p"]
        self.assertTrue(all(p[k] is None for k in ("debt", "debt_lb", "illiquid_lb", "liquid_lo", "liquid_hi", "interest_income", "inv_ub", "total_liabilities")))

    def test_investments_bound_needs_both_rows(self):
        one = bundle_from_yahoo(ystm(dict(GOOD_BS, **{"Other Short Term Investments": 20}), GOOD_PNL))
        self.assertIsNone(one["p"]["inv_ub"])
        both = bundle_from_yahoo(ystm(dict(GOOD_BS, **{"Other Short Term Investments": 20, "Investments And Advances": 50}), GOOD_PNL))
        self.assertEqual(both["p"]["inv_ub"], 70)

    def test_no_balance_sheet_means_no_bundle(self):
        self.assertIsNone(bundle_from_yahoo({"balance_sheet": None, "income_stmt": None}))
        self.assertIsNone(bundle_from_yahoo(None))


# ============================================================ 5) evidence store
class TestEvidence(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.st = EvidenceStore(self.d)

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_roundtrip_and_fiscal_year_filter(self):
        self.st.set("TCS", "haram_business_revenue", 0.0, source="manual", fy_end="2026-03-31", evidence="Segment note 38: no haram segment")
        self.st.set("TCS", "non_compliant_investments", 12.5, source="ai", fy_end="2025-03-31", evidence="Note 7", model="m1")
        used, notes = self.st.for_fy("TCS", "2026-03-31")
        self.assertEqual(used, {"haram_business_revenue": 0.0})
        self.assertEqual(len(notes), 1)
        self.assertIn("ignored", notes[0])

    def test_no_number_without_justification(self):
        bad = [dict(field="nope", value_cr=1.0, source="manual", evidence="x"), dict(field="haram_business_revenue", value_cr=1.0, source="guess", evidence="x"),
               dict(field="haram_business_revenue", value_cr=-1.0, source="manual", evidence="x"), dict(field="haram_business_revenue", value_cr=1.0, source="manual", evidence="  "),
               dict(field="haram_business_revenue", value_cr=None, source="manual", evidence="x")]
        for kw in bad:
            with self.assertRaises(ValueError, msg=str(kw)):
                self.st.set("TCS", fy_end="2026-03-31", **kw)
        self.assertEqual(self.st.get("TCS"), {})

    def test_unknown_symbol_is_empty(self):
        self.assertEqual(self.st.for_fy("NOPE", "2026-03-31"), ({}, []))


# ============================================================ 6) scanner through the real DataManager
class ShProvider:
    name = "shy"

    def __init__(self):
        self.info, self.stm, self.calls, self.fail_info, self.fail_stm = {}, {}, Counter(), set(), set()

    def provider_symbol(self, t):
        return t + ".SH"

    def fetch_info(self, s):
        self.calls["info"] += 1
        if s[:-3] in self.fail_info:
            raise PermanentProviderError("no info")
        return dict(self.info[s[:-3]])

    def fetch_statements(self, s):
        self.calls["stm"] += 1
        if s[:-3] in self.fail_stm:
            raise PermanentProviderError("no statements")
        return self.stm[s[:-3]]


class FakeXbrl:
    name = "bsefake"

    def __init__(self, table):
        self.table, self.calls = table, 0

    def provider_symbol(self, t):
        return t

    def fetch_xbrl_facts(self, code):
        self.calls += 1
        r = self.table[code]
        if isinstance(r, Exception):
            raise r
        return r


INFO = {"marketCap": 5000e7, "sector": "Industrials", "symbol": "x"}


class ScanBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.p = ShProvider()
        for sym, bs, pnl in (("AAA", GOOD_BS, GOOD_PNL), ("BBB", dict(GOOD_BS, **{"Total Debt": 500}), GOOD_PNL), ("CCC", GOOD_BS, GOOD_PNL),
                             ("ITC", GOOD_BS, GOOD_PNL), ("BANKX", {"Total Assets": 100000, "Total Liabilities Net Minority Interest": 92000},
                                                         {"Total Revenue": 10000, "Interest Income": 7000})):
            self.p.info[sym], self.p.stm[sym] = dict(INFO, marketCap=80000e7 if sym == "BANKX" else 5000e7), ystm(bs, pnl)
        self.p.info["DDD"] = dict(INFO)
        self.p.fail_stm.add("DDD")
        self.clock = FakeClock()
        self.dm = DataManager(self.p, self.tmp / "store", FAST, clock=self.clock, sleep=lambda s: None)
        self.ev = EvidenceStore(self.tmp / "ev")
        self.ev.set("CCC", "haram_business_revenue", 0.0, source="manual", fy_end="2026-03-31", evidence="Annual report segment note: nil")
        self.ev.set("CCC", "non_compliant_investments", 20.0, source="manual", fy_end="2026-03-31", evidence="Investment note 9: only 20 Cr in interest-bearing securities")
        self.mapper = BseMapper()
        self.names = ["AAA", "BBB", "CCC", "DDD", "ITC", "BANKX"]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def scan(self, tickers=None, **kw):
        return scan_universe(tickers or self.names, self.dm, self.mapper, self.ev, P, now=NOW, **kw)


class TestScanner(ScanBase):
    def test_verdicts(self):
        res = self.scan()
        v = {r["ticker"]: r["verdict"] for r in res.records}
        self.assertEqual(v, {"AAA": UNDETERMINED, "BBB": NON_COMPLIANT, "CCC": COMPLIANT, "DDD": UNDETERMINED, "ITC": UNDETERMINED, "BANKX": NON_COMPLIANT})
        self.assertEqual(res.meta["by_verdict"], {UNDETERMINED: 3, NON_COMPLIANT: 2, COMPLIANT: 1})

    def test_itc_is_treated_like_any_other_stock(self):
        res = {r["ticker"]: r for r in self.scan().records}
        self.assertEqual(res["ITC"]["metrics"], res["AAA"]["metrics"])
        self.assertEqual(res["ITC"]["verdict"], res["AAA"]["verdict"])

    def test_record_schema_keeps_the_original_fields(self):
        gold = json.loads((ROOT / "golden_master" / "original_outputs" / "shariah_latest_results.json").read_text(encoding="utf-8"))["data"][0]
        rec = self.scan().records[0]
        self.assertTrue(set(gold) <= set(rec), set(gold) - set(rec))
        self.assertEqual(list(rec["metrics"]), CRITERIA)
        for m in rec["metrics"].values():
            self.assertEqual(set(m), {"metric", "threshold", "status"})
            self.assertIn(m["status"], (True, False, None))
        self.assertEqual(rec["is_compliant"], rec["verdict"] == COMPLIANT)
        self.assertEqual(rec["financial_date"], "31 Mar 2026")

    def test_no_statements_still_produces_an_undetermined_record_with_a_note(self):
        res = self.scan(["DDD"])
        r = res.records[0]
        self.assertEqual((r["verdict"], r["data_source"]), (UNDETERMINED, "none"))
        self.assertTrue(all(m["status"] is None for m in r["metrics"].values()))
        self.assertIn("DDD", res.notes)

    def test_info_failure_isolates_the_stock(self):
        self.p.fail_info.add("AAA")
        res = self.scan()
        self.assertIn("AAA", res.failed)
        self.assertEqual(len(res.records), 5)
        self.assertEqual(res.meta["status"], "partial")

    def test_shares_the_cache_with_the_fundamental_screener(self):
        for t in self.names:                                   # what Screener 2 does first
            self.dm.get_info(t)
            if t != "DDD":
                self.dm.get_statements(t)
        before = dict(self.p.calls)
        self.scan()
        self.assertEqual(dict(self.p.calls), {"info": before["info"], "stm": before["stm"] + 1})   # only DDD's failing statements retried

    def test_second_scan_downloads_nothing(self):
        self.scan()
        n = sum(self.dm.ledger.downloads.values())
        self.scan()
        self.assertEqual(sum(self.dm.ledger.downloads.values()), n)

    def test_evidence_from_another_year_is_reported_not_used(self):
        self.ev.set("AAA", "haram_business_revenue", 0.0, source="manual", fy_end="2025-03-31", evidence="old year")
        res = self.scan(["AAA"])
        self.assertEqual(res.records[0]["evidence_used"], [])
        self.assertTrue(any("ignored" in n for n in res.notes["AAA"]))

    def test_output_files_original_format(self):
        res = self.scan()
        paths = write_outputs(res, self.tmp / "out", now=NOW)
        data = json.loads(Path(paths["data"]).read_text(encoding="utf-8"))
        self.assertRegex(data["last_updated"], r"^\d{2} [A-Z][a-z]{2} \d{4}, \d{2}:\d{2} (AM|PM)$")
        self.assertEqual([r["ticker"] for r in data["data"]], sorted(self.names))
        rep = json.loads(Path(paths["report"]).read_text(encoding="utf-8"))
        self.assertEqual(rep["undetermined_by_criterion"][CRITERIA[0]], 5)            # C1 pending for all but CCC (a breach elsewhere does not settle C1)

    def test_carry_forward_and_expiry_and_no_data(self):
        first = self.scan()
        write_outputs(first, self.tmp / "out", now=NOW)
        prev = json.loads((self.tmp / "out" / "latest_results.json").read_text(encoding="utf-8"))["data"]
        before = (self.tmp / "out" / "latest_results.json").read_bytes()
        p2 = ShProvider(); p2.info = self.p.info; p2.stm = self.p.stm; p2.fail_info = set(self.names)
        dm2 = DataManager(p2, self.tmp / "s2", FAST, clock=FakeClock(), sleep=lambda s: None)
        res = scan_universe(self.names, dm2, self.mapper, self.ev, P, now=NOW, previous=prev)
        self.assertEqual(sorted(res.carried), sorted(self.names))
        self.assertTrue(all(r["evaluated_at"] == "2026-11-03T10:00:00Z" for r in res.records))
        later = datetime(2026, 12, 20, tzinfo=timezone.utc)
        res2 = scan_universe(self.names, dm2, self.mapper, self.ev, P, now=later, previous=prev)
        self.assertEqual((res2.carried, res2.records, res2.meta["status"]), ([], [], "no_data"))
        paths = write_outputs(res2, self.tmp / "out", now=later)
        self.assertNotIn("data", paths)
        self.assertEqual((self.tmp / "out" / "latest_results.json").read_bytes(), before)

    def test_full_run_with_universe_service(self):
        uni = self.tmp / "u.txt"
        uni.write_text("\n".join(self.names) + "\n", encoding="utf-8")
        meta = run(UniverseService(uni).get(), self.dm, self.tmp / "res", self.mapper, self.ev, P, now=NOW)
        self.assertEqual((meta["status"], meta["evaluated"], meta["universe_count"]), ("ok", 6, 6))
        self.assertTrue((self.tmp / "res" / "latest_results.json").exists())


class TestXbrlSelection(ScanBase):
    def setUp(self):
        super().setUp()
        self.mapper = BseMapper()
        self.mapper.map["AAA"] = "500001"

    def facts(self, **kw):
        return {"found": True, "facts": parse_xbrl_facts(xbrl_xml(CTX, FACTS, **kw))}

    def rec(self, table):
        x = FakeXbrl(table)
        r, notes = evaluate_stock("AAA", self.dm, self.mapper, self.ev, P, xbrl_provider=x, now=NOW)
        return r, notes, x

    def test_xbrl_wins_a_tie_on_fiscal_year(self):
        r, notes, _ = self.rec({"500001": self.facts(scale="Crores")})
        self.assertEqual((r["data_source"], r["fiscal_year_end"], r["xbrl_scale"]), ("xbrl", "2026-03-31", "crores"))

    def test_newer_yahoo_year_beats_older_xbrl(self):
        old = {"found": True, "facts": parse_xbrl_facts(xbrl_xml(
            {"I1": {"instant": "2025-03-31"}, "D1": {"start": "2024-04-01", "end": "2025-03-31"}},
            [("Assets", "I1", 900), ("RevenueFromOperations", "D1", 700)]))}
        r, _, _ = self.rec({"500001": old})
        self.assertEqual((r["data_source"], r["fiscal_year_end"]), ("yahoo", "2026-03-31"))

    def test_no_filing_or_provider_error_falls_back_to_yahoo_with_a_note(self):
        r, notes, _ = self.rec({"500001": {"found": False}})
        self.assertEqual(r["data_source"], "yahoo")
        self.assertTrue(any("no usable annual XBRL" in n for n in notes))
        self.mapper.map["ITC"] = "500875"                                  # a different stock: AAA's 'not found' answer is (rightly) cached
        x = FakeXbrl({"500875": PermanentProviderError("boom")})
        r2, notes2 = evaluate_stock("ITC", self.dm, self.mapper, self.ev, P, xbrl_provider=x, now=NOW)
        self.assertEqual(r2["data_source"], "yahoo")
        self.assertTrue(any("XBRL unavailable" in n for n in notes2))

    def test_unknown_scale_blocks_only_criterion_five_and_rupee_evidence(self):
        r, _, _ = self.rec({"500001": self.facts()})                      # no scale element in this filing
        self.assertEqual(r["data_source"], "xbrl")
        m = r["metrics"]
        self.assertIsNone(m[CRITERIA[4]]["status"])
        self.assertIs(m[CRITERIA[2]]["status"], True)
        self.assertIs(m[CRITERIA[3]]["status"], True)

    def test_xbrl_cache_found_is_long_missing_is_rechecked_weekly(self):
        x = FakeXbrl({"500001": self.facts(), "500002": {"found": False}})
        self.dm.get_xbrl_facts("AAA", "500001", x)
        self.dm.get_xbrl_facts("BBB", "500002", x)
        self.clock.advance(days=100)
        self.dm.get_xbrl_facts("AAA", "500001", x)
        self.dm.get_xbrl_facts("BBB", "500002", x)
        self.assertEqual(x.calls, 3)                                      # found: cached; missing: re-checked after 7 days
        self.clock.advance(days=-90)


# ============================================================ 7) mapper + BSE adapter
class TestMapper(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_build_from_messy_master_files(self):
        (self.d / "nse.csv").write_text("SYMBOL,NAME OF COMPANY, SERIES, ISIN NUMBER\nAAA,Alpha Ltd,EQ,INE000A01011\nBBB,Beta Ltd,EQ,INE000B01011\nTCS,Tata,EQ,INE467B01029\n", encoding="utf-8")
        (self.d / "bse.csv").write_text("Security Code,Issuer Name,Security Id,ISIN No\n500001,Alpha,ALPHA,INE000A01011\n532540,TCS,TCS,INE467B01029\n", encoding="utf-8")
        rows, rep = build_map(self.d / "nse.csv", self.d / "bse.csv")
        self.assertEqual({r["symbol"]: r["bse_code"] for r in rows}, {"AAA": "500001", "TCS": "532540"})
        self.assertEqual((rep["mapped"], rep["unmatched"], rep["seed_conflicts"]), (2, ["BBB"], []))
        write_map(rows, self.d / "map.csv")
        m = BseMapper(self.d / "map.csv")
        self.assertEqual((m.resolve("aaa"), m.resolve("AAA.NS"), m.resolve("BBB"), m.resolve("RELIANCE")), ("500001", "500001", None, "500325"))

    def test_missing_isin_column_is_an_error_not_a_guess(self):
        (self.d / "bad.csv").write_text("SYMBOL,NAME\nAAA,x\n", encoding="utf-8")
        (self.d / "bse.csv").write_text("Security Code,ISIN No\n1,x\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            build_map(self.d / "bad.csv", self.d / "bse.csv")

    def test_seed_conflict_is_reported(self):
        (self.d / "nse.csv").write_text("SYMBOL,NAME OF COMPANY,ISIN NUMBER\nTCS,Tata,INE467B01029\n", encoding="utf-8")
        (self.d / "bse.csv").write_text("Security Code,ISIN No\n999999,INE467B01029\n", encoding="utf-8")
        _, rep = build_map(self.d / "nse.csv", self.d / "bse.csv")
        self.assertEqual(rep["seed_conflicts"], [("TCS", "532540", "999999")])


class FakeResp:
    def __init__(self, status=200, body=None, content=b"", double=False):
        self.status_code, self._b, self.content, self._double = status, body, content, double

    def json(self):
        return json.dumps(self._b) if self._double else self._b


class FakeSession:
    def __init__(self, api_rows, files, status=200, double=False):
        self.api_rows, self.files, self.status, self.double, self.urls = api_rows, files, status, double, []

    def get(self, url, headers=None, timeout=None, params=None):
        self.urls.append(url)
        if url == BSE_API_URL:
            if self.status != 200:
                return FakeResp(self.status)
            return FakeResp(200, {"Table": self.api_rows if (params or {}).get("pageno") == 1 else []}, double=self.double)
        name = url.rsplit("/", 1)[-1]
        return FakeResp(200, content=self.files[name]) if name in self.files else FakeResp(404)


class TestBseProvider(unittest.TestCase):
    QTR = xbrl_xml(CTX, [("Assets", "I1", 1000), ("RevenueFromOperations", "Q", 200)])

    def zipped(self, xml):
        b = io.BytesIO()
        with zipfile.ZipFile(b, "w") as z:
            z.writestr("filing.xml", xml)
        return b.getvalue()

    def prov(self, rows, files, **kw):
        return BseXbrlProvider(parse_xbrl_facts, session=FakeSession(rows, files, **kw), max_pages=2)

    def test_quarterly_skipped_and_annual_zip_accepted(self):
        rows = [{"ATTACHMENTNAME": "q.xml", "NEWSSUB": "Results"}, {"ATTACHMENTNAME": "a.zip", "NEWSSUB": "Annual results XBRL"}]
        p = self.prov(rows, {"q.xml": self.QTR, "a.zip": self.zipped(xbrl_xml(CTX, FACTS))})
        r = p.fetch_xbrl_facts("500001", datetime(2026, 11, 3))
        self.assertEqual((r["found"], r["filing"]["attachment"], r["facts"]["annual"]), (True, "a.zip", True))

    def test_double_encoded_json_is_understood(self):
        p = self.prov([{"ATTACHMENTNAME": "a.xml", "NEWSSUB": "x"}], {"a.xml": xbrl_xml(CTX, FACTS)}, double=True)
        self.assertTrue(p.fetch_xbrl_facts("1", datetime(2026, 11, 3))["found"])

    def test_only_quarterly_filings_means_not_found(self):
        p = self.prov([{"ATTACHMENTNAME": "q.xml", "NEWSSUB": "Results"}], {"q.xml": self.QTR})
        r = p.fetch_xbrl_facts("1", datetime(2026, 11, 3))
        self.assertEqual((r["found"], r["parsed"]), (False, 1))

    def test_rate_limit_is_raised_for_the_retry_and_breaker_machinery(self):
        with self.assertRaises(RateLimitError):
            self.prov([], {}, status=429).fetch_xbrl_facts("1", datetime(2026, 11, 3))

    def test_nothing_published_means_not_found(self):
        self.assertFalse(self.prov([], {}).fetch_xbrl_facts("1", datetime(2026, 11, 3))["found"])


class TestEvidenceCli(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def run_cli(self, *argv):
        from mizan.screeners.shariah.evidence_cli import main
        import contextlib
        buf, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            code = main(list(argv), store_dir=self.d)
        return code, buf.getvalue(), err.getvalue()

    def test_set_then_show(self):
        code, out, _ = self.run_cli("set", "tcs.ns", "haram_business_revenue", "0", "--fy", "2026-03-31", "--evidence", "Segment note 38: single segment")
        self.assertEqual(code, 0)
        code, out, _ = self.run_cli("show", "TCS")
        self.assertEqual(json.loads(out)["haram_business_revenue"]["value_cr"], 0.0)

    def test_refuses_unjustified_or_invalid_input(self):
        code, _, err = self.run_cli("set", "TCS", "haram_business_revenue", "-5", "--fy", "2026-03-31", "--evidence", "x")
        self.assertEqual(code, 2)
        self.assertIn("not saved", err)
        code, _, err = self.run_cli("set", "BAD SYM", "haram_business_revenue", "1", "--fy", "2026-03-31", "--evidence", "x")
        self.assertEqual(code, 2)
        self.assertEqual(EvidenceStore(self.d).get("TCS"), {})


if __name__ == "__main__":
    unittest.main()
