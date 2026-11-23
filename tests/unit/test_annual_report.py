"""Phase 8 tests - annual-report batch: 100/day cap, token budget, checkpoint recovery, next-day continuation, duplicates, retries."""
import json
import re
import shutil
import sys
import tempfile
import unittest
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mizan.annual_report import (AnnualReportConfig, HARD_STOCK_CAP, LLMOutputError, QueueStore, ReportRef, Usage, UsageStore,  # noqa: E402
                                 AnalysisStore, run_batch, summary)
from mizan.annual_report.extract import build_excerpt, estimate_tokens, score_page, sections_from_xbrl, select_pages  # noqa: E402
from mizan.annual_report.llm import map_llm_exception  # noqa: E402
from mizan.annual_report.prompt import validate_output  # noqa: E402
from mizan.core.errors import ConfigError, RateLimitError, TransientProviderError  # noqa: E402
from mizan.screeners.shariah import COMPLIANT, EvidenceStore, evaluate  # noqa: E402
from mizan.screeners.shariah.mapper import BseMapper  # noqa: E402

BASE = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)


class Crash(BaseException):
    """Simulates the process dying mid-batch (not an Exception: nothing in the engine may swallow it)."""


def note_xml(extra="") -> bytes:
    text = ("Other income comprises interest income on bank deposits of Rs 5.0 crore and other receipts. Borrowings are secured. " * 4) + extra
    return (f'<?xml version="1.0"?><xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:n="http://x">'
            f'<xbrli:context id="c"><xbrli:period><xbrli:instant>2026-03-31</xbrli:instant></xbrli:period></xbrli:context>'
            f'<n:DisclosureOfOtherIncomeExplanatory contextRef="c">{text}</n:DisclosureOfOtherIncomeExplanatory>'
            f'<n:Assets contextRef="c">1000</n:Assets></xbrli:xbrl>').encode()


NUMERIC_ONLY = b'<?xml version="1.0"?><xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:n="http://x"><n:Assets contextRef="c">1000</n:Assets></xbrli:xbrl>'
OK = {"fiscal_year_end": "2026-03-31",
      "haram_business_revenue": {"value": 0.0, "evidence": "Segment note: no excluded segments"},
      "impermissible_interest_income": {"value": 5.0, "evidence": "Note 25: interest income 5.0"},
      "non_compliant_investments": {"value": 20.0, "evidence": "Note 7: investments 20.0"}}


class FakeLLM:
    model = "fake-model"

    def __init__(self, tokens=(1000, 100)):
        self.calls, self.n, self.script, self.tokens, self.payload, self.fail_at = Counter(), 0, {}, tokens, dict(OK), {}

    def generate_json(self, prompt, max_output_tokens):
        sym = re.search(r"\((\S+)\)\. Fiscal", prompt).group(1)
        self.n += 1; self.calls[sym] += 1
        s = self.script.get(sym, [])
        beh = self.fail_at.get(self.n) or (s.pop(0) if s else "ok")
        if beh == "bad_json":
            raise LLMOutputError("not json", Usage(500, 50))
        if beh == "rate":
            raise RateLimitError("429 RESOURCE_EXHAUSTED")
        if beh == "transient":
            raise TransientProviderError("503")
        if beh == "crash":
            raise Crash()
        return dict(self.payload), Usage(*self.tokens)


class FakeReports:
    def __init__(self, syms, fy="2026-03-31"):
        self.versions = {s: "v1" for s in syms}
        self.fy, self.missing, self.calls, self.find_error, self.pdf, self.content = fy, set(), Counter(), None, {}, {}

    def find(self, sym, code):
        self.calls["find"] += 1
        if self.find_error:
            raise self.find_error
        if sym in self.missing:
            return None
        return ReportRef("xbrl", f"{sym}.xml", "2026-06-01", self.fy, f"{self.versions[sym]}-{sym}", "bse_xbrl")

    def find_pdf(self, sym, code):
        return self.pdf.get(sym)

    def fetch(self, ref):
        self.calls["fetch"] += 1
        return self.content.get(ref.attachment, note_xml())


class NameDM:
    def get_info(self, sym):
        return SimpleNamespace(data={"longName": f"{sym} Limited"})


class Base(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.names = [f"S{i:03d}" for i in range(30)]
        self.llm, self.rep = FakeLLM(), FakeReports(self.names)
        self.ev = EvidenceStore(self.d / "state" / "shariah_evidence")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def cfg(self, **kw):
        d = dict(daily_token_budget=10_000_000, requests_per_minute=600, discovery_per_day=1000, discovery_requests_per_minute=600)
        d.update(kw)
        return AnnualReportConfig(**d)

    def run_day(self, offset=0, names=None, cfg=None, hours=0, **kw):
        return run_batch(names or self.names, store_dir=self.d, provider=self.rep, llm=self.llm, evidence=self.ev, dm=NameDM(),
                         mapper=BseMapper(), cfg=cfg or self.cfg(), now=BASE + timedelta(days=offset, hours=hours), sleep=lambda s: None, **kw)

    def queue(self):
        return QueueStore(self.d / "state" / "annual_report" / "queue.json")

    def usage(self):
        return UsageStore(self.d / "state" / "annual_report" / "usage.json")


class TestLimitsAndConfig(Base):
    def test_refuses_to_run_without_budget_or_rate(self):
        for kw in ({"daily_token_budget": None}, {"requests_per_minute": None}):
            with self.assertRaises(ConfigError):
                self.run_day(cfg=self.cfg(**kw))
        self.assertFalse((self.d / "state" / "annual_report" / "queue.json").exists())
        self.assertEqual(self.llm.n, 0)

    def test_daily_stock_cap_can_never_exceed_100(self):
        self.assertEqual(AnnualReportConfig(max_stocks_per_day=500).stock_cap, HARD_STOCK_CAP)
        self.assertEqual(AnnualReportConfig(max_stocks_per_day=40).stock_cap, 40)

    def test_config_loaded_from_app_yaml_dict(self):
        c = AnnualReportConfig.from_config({"annual_report": {"daily_token_budget": 5, "requests_per_minute": 2, "retry_days": [1, 2], "on_rate_limit": "x"}})
        self.assertEqual((c.daily_token_budget, c.requests_per_minute, c.retry_days), (5, 2, (1, 2)))


class TestHundredPerDayAndContinuation(Base):
    def test_100_per_day_then_continues_next_days_without_repeating(self):
        names = [f"S{i:03d}" for i in range(250)]
        self.rep = FakeReports(names)
        r0 = self.run_day(0, names)
        self.assertEqual((len(r0.processed), r0.stopped), (100, "stock_limit"))
        r1 = self.run_day(1, names)
        self.assertEqual((len(r1.processed), r1.stopped), (100, "stock_limit"))
        self.assertFalse(set(r0.processed) & set(r1.processed))
        r2 = self.run_day(2, names)
        self.assertEqual((len(r2.processed), r2.stopped), (50, "completed"))
        self.assertEqual(sum(self.llm.calls.values()), 250)
        self.assertTrue(all(c == 1 for c in self.llm.calls.values()))                  # every stock analysed exactly once
        self.assertEqual(summary(self.queue(), self.usage(), self.cfg(), BASE + timedelta(days=2))["reports_processed"], 250)

    def test_second_run_same_day_respects_the_same_cap(self):
        names = [f"S{i:03d}" for i in range(250)]
        self.rep = FakeReports(names)
        self.run_day(0, names)
        r = self.run_day(0, names, hours=3)
        self.assertEqual((len(r.processed), r.stopped), (0, "stock_limit"))
        self.assertEqual(sum(self.llm.calls.values()), 100)


class TestTokenBudget(Base):
    def test_never_starts_a_call_that_could_exceed_the_budget_and_resumes_tomorrow(self):
        cfg = self.cfg(daily_token_budget=5000)
        r0 = self.run_day(0, cfg=cfg)
        self.assertEqual(r0.stopped, "token_budget")
        self.assertLessEqual(r0.tokens_in + r0.tokens_out, 5000)
        self.assertTrue(0 < len(r0.processed) < len(self.names))
        left = [s for s in self.names if s not in r0.processed]
        q = self.queue()
        self.assertTrue(all(q.entries[s]["status"] == "pending" and q.entries[s]["attempts"] == 0 for s in left))   # untouched, no failure counted
        r1 = self.run_day(1, cfg=cfg)
        self.assertFalse(set(r0.processed) & set(r1.processed))
        self.assertGreater(len(r1.processed), 0)

    def test_overshoot_by_the_provider_stops_the_batch_at_once(self):
        self.llm.tokens = (6000, 100)                                    # first call alone passes the 5,000 budget
        r = self.run_day(0, cfg=self.cfg(daily_token_budget=5000))
        self.assertEqual((len(r.processed), r.stopped), (1, "token_budget"))
        self.assertEqual(self.llm.n, 1)

    def test_budget_is_per_calendar_day_across_runs(self):
        cfg = self.cfg(daily_token_budget=5000)
        a = self.run_day(0, cfg=cfg)
        b = self.run_day(0, cfg=cfg, hours=2)                            # second run the same day finds the budget used up
        self.assertEqual(len(b.processed), 0)
        self.assertEqual(b.stopped, "token_budget")

    def test_warning_alert_at_80_percent(self):
        r = self.run_day(0, cfg=self.cfg(daily_token_budget=4000))      # 3 calls fit (82% used), a 4th would not
        self.assertTrue(any("daily budget" in a for a in r.alerts))
        self.assertTrue(any("stopped early" in a for a in r.alerts))


class TestCheckpointRecovery(Base):
    def crash_after(self, n):
        self.llm.fail_at = {n + 1: "crash"}

    def test_crash_loses_only_the_stock_in_progress(self):
        self.crash_after(10)
        with self.assertRaises(Crash):
            self.run_day(0)
        q = self.queue()
        self.assertEqual(q.counts().get("done"), 10)
        self.assertEqual(q.counts().get("processing"), 1)
        self.assertEqual(self.usage().data[BASE.strftime("%Y-%m-%d")]["stocks"], 10)
        self.assertEqual(len(list((self.d / "state" / "annual_report" / "analyses").glob("*.json"))), 10)
        self.llm.fail_at = {}
        r = self.run_day(0, force_recover=True)
        self.assertEqual(len(r.recovered), 1)
        self.assertEqual(len(r.processed), 20)                           # the interrupted one + the other 19; the first 10 are NOT redone
        calls = self.llm.calls
        self.assertEqual(sorted(calls.values()).count(2), 1)             # only the crashed stock was called twice
        self.assertEqual(self.queue().counts(), {"done": 30})

    def test_fresh_processing_entry_is_left_alone_unless_forced(self):
        self.crash_after(2)
        with self.assertRaises(Crash):
            self.run_day(0)
        stuck = [s for s, e in self.queue().entries.items() if e["status"] == "processing"][0]
        self.llm.fail_at = {}
        r = self.run_day(0, hours=1)                                     # inside the 6 h timeout: a live run might own it
        self.assertEqual(r.recovered, [])
        self.assertEqual(self.queue().entries[stuck]["status"], "processing")
        r = self.run_day(0, hours=7)                                     # past the timeout: treated as crashed
        self.assertEqual(r.recovered, [stuck])
        self.assertIn(stuck, r.processed)


class TestNoDuplicateOrUnneededWork(Base):
    def test_finished_queue_makes_no_ai_calls_and_no_lookups(self):
        self.run_day(0)
        n_llm, n_find = self.llm.n, self.rep.calls["find"]
        r = self.run_day(1)
        self.assertEqual((r.processed, r.discovered, self.llm.n, self.rep.calls["find"]), ([], 0, n_llm, n_find))
        self.assertEqual(r.stopped, "completed")

    def test_same_report_version_is_never_analysed_twice_but_a_new_version_is(self):
        self.run_day(0)
        n = self.llm.n
        r = self.run_day(301)                                            # annual re-check finds the SAME versions
        self.assertEqual((r.discovered, self.llm.n), (30, n))
        self.rep.versions["S003"] = "v2"                                 # a revised / new annual report
        r = self.run_day(602)
        self.assertEqual(r.processed, ["S003"])
        self.assertEqual(self.llm.calls["S003"], 2)
        self.assertEqual(len(AnalysisStore(self.d / "state" / "annual_report" / "analyses").records("S003")), 2)

    def test_no_report_stocks_are_rechecked_only_after_30_days(self):
        self.rep.missing = {"S001", "S002"}
        r = self.run_day(0)
        self.assertEqual(self.queue().counts()["no_report"], 2)
        f = self.rep.calls["find"]
        self.run_day(10); self.assertEqual(self.rep.calls["find"], f)
        self.rep.missing = set()
        r = self.run_day(31)
        self.assertEqual(sorted(r.processed), ["S001", "S002"])

    def test_discovery_budget_is_separate_and_paces_the_queue(self):
        cfg = self.cfg(discovery_per_day=10)
        r0 = self.run_day(0, cfg=cfg)
        self.assertEqual((r0.discovered, len(r0.processed)), (10, 10))
        r1 = self.run_day(1, cfg=cfg)
        self.assertEqual((r1.discovered, len(r1.processed)), (10, 10))

    def test_discovery_rate_limit_pauses_lookups_but_not_analysis_of_known_reports(self):
        self.run_day(0, cfg=self.cfg(discovery_per_day=5))
        self.rep.find_error = RateLimitError("429")
        r = self.run_day(1)
        self.assertEqual(r.discovered, 0)
        self.assertTrue(any("look-up paused" in a for a in r.alerts))

    def test_stocks_leaving_and_rejoining_the_universe(self):
        self.run_day(0)
        r = self.run_day(1, names=self.names[:20])
        self.assertEqual(self.queue().counts()["removed"], 10)
        self.assertEqual(summary(self.queue(), self.usage(), self.cfg(), BASE)["total_eligible_stocks"], 20)
        n = self.llm.n
        self.run_day(2, names=self.names)                                # back again: history kept, nothing re-analysed
        self.assertEqual((self.llm.n, self.queue().counts().get("done")), (n, 30))


class TestFailuresAndRetries(Base):
    def test_failed_report_retry_schedule_and_final_failure(self):
        self.llm.script["S000"] = ["bad_json"] * 10
        names = ["S000"]
        self.rep = FakeReports(names)
        offsets = [0, 1, 4, 11, 25]                                       # +1, +3, +7, +14 days between attempts
        for i, off in enumerate(offsets, 1):
            self.run_day(off, names)
            e = self.queue().entries["S000"]
            self.assertEqual(e["attempts"], i)
            self.assertEqual(e["status"], "failed_final" if i == 5 else "failed")
        calls = self.llm.calls["S000"]
        self.run_day(60, names)
        self.assertEqual(self.llm.calls["S000"], calls)                   # permanently failed: never retried automatically
        self.assertTrue(self.usage().data[BASE.strftime("%Y-%m-%d")]["tokens_in"] >= 500)   # tokens burnt by bad output are counted

    def test_failed_stock_is_not_retried_the_same_day_but_the_others_continue(self):
        self.llm.script["S005"] = ["bad_json"]
        r = self.run_day(0)
        self.assertEqual((list(r.failed), len(r.processed)), (["S005"], 29))
        self.run_day(0, hours=3)
        self.assertEqual(self.llm.calls["S005"], 1)
        r = self.run_day(1)
        self.assertEqual(r.processed, ["S005"])
        e = self.queue().entries["S005"]
        self.assertEqual((e["status"], e["attempts"], e["last_error"]), ("done", 0, None))

    def test_ai_rate_limit_pauses_the_batch_without_counting_a_failure(self):
        self.llm.fail_at = {5: "rate"}
        r = self.run_day(0)
        self.assertEqual((len(r.processed), r.stopped), (4, "rate_limited"))
        paused = [s for s, e in self.queue().entries.items() if e["status"] == "pending"]
        self.assertEqual(len(paused), 26)
        self.assertTrue(all(self.queue().entries[s]["attempts"] == 0 for s in paused))
        r2 = self.run_day(1)
        self.assertEqual(len(r2.processed), 26)
        self.assertEqual(sorted(self.llm.calls.values()).count(2), 1)       # only the stock that hit the limit was called twice

    def test_transient_ai_error_is_retried_inside_the_run(self):
        self.llm.script["S000"] = ["transient"]
        self.run_day(0)
        self.assertEqual(self.queue().entries["S000"]["status"], "done")
        self.assertEqual(self.llm.calls["S000"], 2)

    def test_download_failure_counts_an_attempt(self):
        def boom(ref):
            raise TransientProviderError("download failed")
        self.rep.fetch = boom
        r = self.run_day(0, names=["S000"])
        self.assertIn("S000", r.failed)
        self.assertEqual(self.llm.n, 0)


class TestResultsAndEvidence(Base):
    def test_only_validated_fields_reach_the_shariah_evidence_store(self):
        self.llm.payload = {"fiscal_year_end": "2026-03-31", "haram_business_revenue": {"value": None, "evidence": ""},
                            "impermissible_interest_income": {"value": 5.0, "evidence": "Note 25"},
                            "non_compliant_investments": {"value": 20.0, "evidence": ""}}
        self.run_day(0, names=["S000"])
        used, _ = self.ev.for_fy("S000", "2026-03-31")
        self.assertEqual(used, {"impermissible_interest_income": 5.0})        # null stays pending; value without evidence is rejected
        rec = AnalysisStore(self.d / "state" / "annual_report" / "analyses").records("S000")[0]
        self.assertEqual(sorted(rec["fields_rejected"]), ["haram_business_revenue", "non_compliant_investments"])
        self.assertEqual(self.ev.get("S000")["impermissible_interest_income"]["source"], "ai")
        self.assertEqual(self.ev.get("S000")["impermissible_interest_income"]["model"], "fake-model")

    def test_all_null_is_inconclusive_not_zero(self):
        self.llm.payload = {"fiscal_year_end": "2026-03-31", **{k: {"value": None, "evidence": ""} for k in
                            ("haram_business_revenue", "impermissible_interest_income", "non_compliant_investments")}}
        self.run_day(0, names=["S000"])
        self.assertEqual(self.ev.get("S000"), {})
        s = summary(self.queue(), self.usage(), self.cfg(), BASE)
        self.assertEqual((s["reports_processed"], s["inconclusive"]), (1, 1))

    def test_ai_evidence_flows_into_the_shariah_verdict(self):
        self.run_day(0, names=["S000"])
        used, _ = self.ev.for_fy("S000", "2026-03-31")
        p = dict(total_assets=1000.0, total_revenue=1000.0, total_liabilities=500.0, debt=100.0, debt_lb=100.0, illiquid_lb=400.0,
                 illiquid_exact=400.0, liquid_lo=300.0, liquid_hi=300.0, liquid_exact=300.0, interest_income=None, inv_ub=None)
        self.assertEqual(evaluate(p, 1.0, 5000.0, used)[0], COMPLIANT)   # C1 pending before the AI result, compliant after
        self.assertNotEqual(evaluate(p, 1.0, 5000.0, {})[0], COMPLIANT)

    def test_saved_analysis_has_every_field_the_requirements_list(self):
        self.run_day(0, names=["S000"])
        rec = AnalysisStore(self.d / "state" / "annual_report" / "analyses").records("S000")[0]
        for k in ("symbol", "company_name", "fiscal_year_end", "report_source", "report_version", "download_date", "analysis_date",
                  "status", "ai_model", "tokens_in", "tokens_out", "output"):
            self.assertIn(k, rec)
        self.assertEqual((rec["company_name"], rec["report_source"], rec["status"], rec["tokens_in"]), ("S000 Limited", "bse_xbrl", "done", 1000))

    def test_validate_output_rules(self):
        ok, bad = validate_output({"haram_business_revenue": {"value": 0, "evidence": "nil per note"},
                                   "impermissible_interest_income": {"value": -1, "evidence": "x"},
                                   "non_compliant_investments": {"value": True, "evidence": "x"}})
        self.assertEqual(ok, {"haram_business_revenue": (0.0, "nil per note")})
        self.assertEqual(set(bad), {"impermissible_interest_income", "non_compliant_investments"})

    def test_pdf_is_only_a_fallback_for_xml_without_note_text(self):
        self.rep.content["S000.xml"] = NUMERIC_ONLY
        pdf = ReportRef("pdf", "S000.pdf", "2026-06-01", None, "pdfv1", "bse_pdf")
        self.rep.pdf["S000"] = pdf
        self.rep.content["S000.pdf"] = b"%PDF"
        with mock.patch("mizan.annual_report.engine.pdf_pages", return_value=["Segment reporting ... other income ... borrowings"]):
            self.run_day(0, names=["S000"])
        rec = AnalysisStore(self.d / "state" / "annual_report" / "analyses").records("S000")[0]
        self.assertEqual((rec["report_source"], rec["fiscal_year_end"]), ("bse_pdf", "2026-03-31"))   # FY taken from the AI's reading of the PDF

    def test_no_text_and_no_fallback_is_a_failure_not_a_guess(self):
        self.rep.content["S000.xml"] = NUMERIC_ONLY
        r = self.run_day(0, names=["S000"], cfg=self.cfg(allow_pdf_fallback=False))
        self.assertIn("no usable note text", r.failed["S000"])
        self.assertEqual(self.llm.n, 0)


class TestDashboardNumbers(Base):
    def test_numbers_are_consistent(self):
        self.rep.missing = {"S001", "S002", "S003"}
        self.llm.script["S010"] = ["bad_json"]
        self.run_day(0, cfg=self.cfg(max_stocks_per_day=10, daily_token_budget=1_000_000))
        s = json.loads((self.d / "results" / "annual_report" / "dashboard.json").read_text())
        self.assertEqual(s["total_eligible_stocks"], 30)
        self.assertEqual(s["no_report_found"], 3)
        self.assertEqual(s["reports_available"], 27)
        self.assertEqual(s["reports_processed"] + s["reports_failed"], 10)                     # the day's 10 stocks
        self.assertEqual(s["reports_available"], s["reports_processed"] + s["reports_pending"] + s["reports_failed"])   # categories add up
        self.assertEqual((s["daily_batch"]["stocks_processed"], s["daily_batch"]["stocks_limit"], s["daily_batch"]["stopped_reason"]), (10, 10, "stock_limit"))
        self.assertEqual(s["tokens"]["today_total"], s["tokens"]["today_in"] + s["tokens"]["today_out"])
        self.assertEqual(s["last_successful_processing_date"], "2026-11-09")
        self.assertTrue(s["next_scheduled_batch"].startswith("2026-11-10"))
        self.assertEqual(s["estimated_days_to_finish_upper_bound"], 2)                         # 17 reports left at 10/day
        self.assertEqual(s["failed_detail"][0]["symbol"], "S010")

    def test_nothing_left_means_no_next_batch(self):
        self.run_day(0)
        s = summary(self.queue(), self.usage(), self.cfg(), BASE + timedelta(days=1))
        self.assertIsNone(s["next_scheduled_batch"])


class TestExtractionAndClient(unittest.TestCase):
    def test_page_ranking_beats_table_of_contents_and_respects_cap(self):
        toc = "\n".join(f"Borrowings .... {i}" for i in range(30))
        real = "Note 17 Borrowings\nNote 25 Other income includes interest income\nSegment reporting follows"
        pages = [toc] + ["blank"] * 5 + [real] + [f"Current investments note {i}" for i in range(12)]
        sel = select_pages(pages, max_pages=8)
        self.assertEqual(len(sel), 8)
        self.assertIn(7, [n for n, _ in sel])                               # the real notes page is kept
        self.assertNotIn(1, [n for n, _ in sel])                            # the contents page is not
        self.assertEqual([n for n, _ in sel], sorted(n for n, _ in sel))    # page order preserved
        self.assertLess(score_page(toc), score_page(real))

    def test_xbrl_text_blocks_only_and_excerpt_cap(self):
        secs = sections_from_xbrl(note_xml())
        self.assertEqual(len(secs), 1)                                      # numeric facts and short text are ignored
        self.assertIn("Other income", secs[0])
        self.assertLessEqual(len(build_excerpt(["x" * 500, "y" * 500], 600)), 600 + 2)
        self.assertEqual(sections_from_xbrl(b"<not xml"), [])
        self.assertEqual(estimate_tokens("a" * 300, 3.0), 101)

    def test_llm_errors_are_classified(self):
        class E(Exception):
            code = 429
        self.assertIsInstance(map_llm_exception(E("busy")), RateLimitError)
        self.assertIsInstance(map_llm_exception(RuntimeError("RESOURCE_EXHAUSTED quota")), RateLimitError)
        self.assertIsInstance(map_llm_exception(TimeoutError("timed out")), TransientProviderError)


if __name__ == "__main__":
    unittest.main()
