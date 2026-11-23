"""Phase 9 tests (2/2): built-in modules end-to-end, view-models, all pages (viewer + admin), admin actions, app wiring."""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
from dash_helpers import ADMIN, make_ctx  # noqa: E402
from fake_st import FakeSt  # noqa: E402
from mizan.core.auth import Authenticator, VIEWER, hash_password  # noqa: E402
from mizan.dashboard import app as appmod, views as V  # noqa: E402
from mizan.execution import HistoryStore, run_modules  # noqa: E402
from mizan.modules.registry import ModuleSpec  # noqa: E402

CORE_KEYS = ["overview", "technical", "fundamental", "shariah", "annual_report", "data_management", "execution_monitor",
             "custom_screeners", "custom_analysis", "system_config"]


class Shared(unittest.TestCase):
    """One real run of the four built-in modules, shared by the read-only tests."""

    @classmethod
    def setUpClass(cls):
        cls.d = Path(tempfile.mkdtemp())
        cls.ctx, cls.auth, cls.prov = make_ctx(cls.d)
        cls.outs = {o.module_id: o for o in run_modules(cls.ctx.registry, cls.ctx)}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.d, ignore_errors=True)

    def page(self, key, session=VIEWER, st=None, ctx=None):
        st = st or FakeSt()
        appmod.render_page(st, ctx or self.ctx, session, key)
        return st


class TestBuiltinModulesEndToEnd(Shared):
    def test_statuses(self):
        self.assertEqual({k: o.status for k, o in self.outs.items()},
                         {"technical": "ok", "fundamental": "ok", "shariah": "ok", "annual_report": "not_configured"})
        self.assertIn("daily_token_budget", self.outs["annual_report"].error)             # the open limits are reported, not crashed on

    def test_each_declared_output_schema_matches_what_the_engines_really_write(self):
        for mid in ("technical", "fundamental", "shariah"):
            self.assertEqual(self.outs[mid].output_problems, [], mid)
            self.assertTrue(self.ctx.registry.load_rows(self.ctx.registry.get(mid), self.ctx))

    def test_three_screeners_share_downloads_with_no_duplicates(self):
        v = V.data_management_view(self.ctx.store, self.ctx.universe_service, today="2026-11-03")
        self.assertEqual(v["duplicates_today"], [])
        by = {(r["data_type"]): r["download_count"] for r in v["downloads_today"]}
        self.assertTrue(all(c == 1 for c in [r["download_count"] for r in v["downloads_today"]]))
        self.assertEqual(sum(1 for r in v["downloads_today"] if r["data_type"] == "ohlcv"), 5)     # one price download per stock for three screeners


class TestViews(Shared):
    def test_technical(self):
        v = V.technical_view(self.ctx.store)
        self.assertFalse(v["empty"])
        for k in ("actionable", "watch", "all"):
            self.assertIsInstance(v[k], list)
        if v["actionable"]:
            self.assertIn("Notify at (₹)", v["actionable"][0])

    def test_fundamental_sections_cover_every_row(self):
        v = V.fundamental_view(self.ctx.store)
        self.assertEqual(sum(len(r) for _, _, r in v["left"] + v["right"]), len(v["rows"]))
        self.assertEqual([h for h, _, _ in v["left"]], ["🔥 Execution Ready (Pass)", "⏳ Waiting for Price", "💰 Waiting for Valuation"])
        self.assertEqual([h for h, _, _ in v["right"]], ["👀 Watchlist (Near Buy Zone)", "⚠️ Fundamental Wait (Fails Criteria)"])

    def test_shariah_kpis_cards_and_verdicts(self):
        v = V.shariah_view(self.ctx.store)
        k = v["kpis"]
        self.assertEqual(k["total"], 5)
        self.assertEqual(k["compliant"] + k["non_compliant"] + k["undetermined"], 5)
        by = {i["ticker"]: i for i in v["items"]}
        self.assertEqual(by["BANK"]["verdict"], "NON_COMPLIANT")                      # interest business filtered out
        self.assertEqual(by["BBB"]["verdict"], "NON_COMPLIANT")
        self.assertEqual(by["AAA"]["verdict"], "UNDETERMINED")                        # C1 evidence pending
        self.assertTrue(all(len(i["cards"]) == 6 and {c["state"] for c in i["cards"]} <= {"pass", "fail", "pending"} for i in v["items"]))
        self.assertEqual(k["rate"], round(k["compliant"] / 5 * 100, 1))

    def test_overview_matches_the_screeners(self):
        o = V.overview_view(self.ctx.store)
        self.assertEqual(o["shariah"], V.shariah_view(self.ctx.store)["kpis"])
        self.assertEqual(len(o["cross"]["rows"]), 5)

    def test_execution_and_config(self):
        e = V.execution_view(self.ctx.store, self.ctx.registry)
        self.assertEqual(len(e["history"]), 4)
        self.assertEqual(set(e["reports"]), {"technical", "fundamental", "shariah"})
        c = V.config_view(self.ctx.cfg, self.ctx.registry, self.ctx.universe_service)
        self.assertIn("near_52w_high", [m["id"] for m in c["modules"]])

    def test_empty_store_never_raises(self):
        d = Path(tempfile.mkdtemp())
        try:
            self.assertTrue(V.technical_view(d)["empty"] and V.fundamental_view(d)["empty"] and V.shariah_view(d)["empty"] and V.annual_view(d)["empty"])
            self.assertTrue(V.overview_view(d)["empty"])
            self.assertEqual(V.data_management_view(d)["size"]["files"], 0)
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestPages(Shared):
    def test_every_page_renders_for_viewer_and_admin_without_error(self):
        for session in (VIEWER, ADMIN):
            for key in CORE_KEYS:
                st = self.page(key, session)
                self.assertNotIn("This page failed", st.texts("error"), f"{key} / {session.role}")
                self.assertTrue(st.has("title") or st.has("markdown"), key)

    def test_technical_page_keeps_the_original_design(self):
        st = self.page("technical")
        self.assertIn("High-Probability Setup Dashboard", st.texts("title"))
        self.assertEqual(st.args("tabs")[0][0], ["⭐ Actionable (A/B)", "👀 Watch (C)", "🗂️ All setups (original view)"])

    def test_fundamental_page_sections_and_corrected_caption(self):
        st = self.page("fundamental")
        self.assertIn("Quality Value Rebound", st.texts("title"))
        self.assertIn("Fundamental Wait", st.texts("subheader"))
        self.assertIn("35-65%", st.texts("caption") + st.texts("info"))
        self.assertNotIn("50-70%", st.texts())                                           # the old wrong caption is gone

    def test_shariah_page_keeps_dark_design_and_adds_undetermined(self):
        st = self.page("shariah")
        body = st.texts("markdown")
        self.assertIn("<style>", body)
        self.assertIn("badge-undetermined", body)
        self.assertIn("Undetermined", body)
        self.assertIn("Shariah Screen of NSE/BSE Stocks", st.texts("title"))
        self.assertEqual(sum(1 for a in st.args("expander") if "Period" in a[0]), 5)       # one expander per stock

    def test_admin_only_controls(self):
        v = {k: self.page(k, VIEWER) for k in ("technical", "shariah", "execution_monitor", "system_config")}
        a = {k: self.page(k, ADMIN) for k in ("technical", "shariah", "execution_monitor", "system_config")}
        self.assertNotIn("🚀 Run Swing Setup Radar", v["technical"].labels())
        self.assertIn("🚀 Run Swing Setup Radar", a["technical"].labels())
        self.assertIn("🚀 Run Full Shariah Screen", a["shariah"].labels())
        self.assertFalse(any("Run all" in l for l in v["execution_monitor"].labels()))
        self.assertTrue(any("Run all" in l for l in a["execution_monitor"].labels()))
        self.assertFalse(any("evidence" in str(x[0]) for x in v["shariah"].args("expander")))
        self.assertTrue(any("evidence" in str(x[0]) for x in a["shariah"].args("expander")))
        self.assertFalse(v["system_config"].has("text_area") or v["system_config"].has("file_uploader"))
        self.assertTrue(a["system_config"].has("text_area") and a["system_config"].has("file_uploader"))
        self.assertIn("Only the admin", v["technical"].texts("caption"))

    def test_annual_report_page_shows_the_open_limits_clearly(self):
        st = self.page("annual_report")
        self.assertIn("Not configured yet", st.texts("warning"))
        self.assertIn("No annual-report batch has run yet", st.texts("info"))

    def test_data_management_reports_no_duplicates(self):
        self.assertIn("No duplicate downloads today", self.page("data_management").texts("success") + self.page("data_management").texts("error"))


class TestAdminActions(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.ctx, self.auth, _ = make_ctx(self.d)

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def universe(self):
        return self.ctx.universe_service.get().tickers

    def test_admin_runs_a_module_from_its_page_and_viewer_cannot(self):
        appmod.render_page(FakeSt(press={"run_shariah"}), self.ctx, VIEWER, "shariah")
        self.assertFalse((self.ctx.results_dir("shariah") / "latest_results.json").exists())
        st = FakeSt(press={"run_shariah"})
        appmod.render_page(st, self.ctx, ADMIN, "shariah")
        self.assertTrue((self.ctx.results_dir("shariah") / "latest_results.json").exists())
        self.assertEqual(HistoryStore(self.ctx.store / "state" / "execution" / "history.json").last_by_module()["shariah"]["status"], "ok")

    def test_admin_adds_new_ipo_symbols_by_text_and_by_csv_upload(self):
        st = FakeSt(inputs={"uni_add": "NEWIPO\nBAD SYM"}, press={"uni_btn"})
        appmod.render_page(st, self.ctx, ADMIN, "system_config")
        self.assertIn("NEWIPO", self.universe())
        self.assertIn("BAD SYM", st.texts("error"))                                    # rejected, with the reason
        up = type("U", (), {"getvalue": lambda s: b"Company,Symbol\nBig IPO,BIGIPO\nOther,OTHERIPO\n"})()
        appmod.render_page(FakeSt(inputs={"uni_file": up}, press={"uni_btn"}), self.ctx, ADMIN, "system_config")
        self.assertTrue({"BIGIPO", "OTHERIPO"} <= set(self.universe()))

    def test_ambiguous_csv_is_refused_with_a_message(self):
        up = type("U", (), {"getvalue": lambda s: b"Company,Sector\nA,IT\nB,IT\n"})()
        st = FakeSt(inputs={"uni_file": up}, press={"uni_btn"})
        appmod.render_page(st, self.ctx, ADMIN, "system_config")
        self.assertIn("Refusing to guess", st.texts("error"))

    def test_admin_removes_disables_and_enables(self):
        appmod.render_page(FakeSt(inputs={"uni_sym": "AAA"}, press={"uni_dis"}), self.ctx, ADMIN, "system_config")
        self.assertNotIn("AAA", self.universe())
        appmod.render_page(FakeSt(inputs={"uni_sym": "AAA"}, press={"uni_en"}), self.ctx, ADMIN, "system_config")
        self.assertIn("AAA", self.universe())
        appmod.render_page(FakeSt(inputs={"uni_sym": "BBB"}, press={"uni_rm"}), self.ctx, ADMIN, "system_config")
        self.assertNotIn("BBB", self.universe())

    def test_viewer_cannot_change_the_universe_even_with_crafted_input(self):
        appmod.render_page(FakeSt(inputs={"uni_add": "HACK"}, press={"uni_btn"}), self.ctx, VIEWER, "system_config")
        self.assertNotIn("HACK", self.universe())

    def test_evidence_entry_requires_a_justification(self):
        run_modules(self.ctx.registry, self.ctx, ["shariah"])
        inputs = {"ev_sym": "AAA", "ev_field": "haram_business_revenue", "ev_val": 0.0, "ev_fy": "2026-03-31", "ev_why": ""}
        st = FakeSt(inputs=inputs, press={"ev_save"})
        appmod.render_page(st, self.ctx, ADMIN, "shariah")
        self.assertIn("evidence text is required", st.texts("error"))
        self.assertEqual(self.ctx.evidence().get("AAA"), {})
        inputs["ev_why"] = "Segment note 34: no excluded segments"
        st = FakeSt(inputs=inputs, press={"ev_save"})
        appmod.render_page(st, self.ctx, ADMIN, "shariah")
        self.assertEqual(self.ctx.evidence().get("AAA")["haram_business_revenue"]["source"], "manual")

    def test_admin_can_disable_a_module_and_it_stays_disabled(self):
        appmod.render_page(FakeSt(inputs={"mod_technical": False}), self.ctx, ADMIN, "system_config")
        self.assertFalse(self.ctx.registry.is_enabled("technical"))
        self.assertEqual(self.ctx.registry.run("technical", self.ctx).status, "disabled")
        appmod.render_page(FakeSt(inputs={"mod_technical": False}), self.ctx, VIEWER, "system_config")      # a viewer's page changes nothing
        self.assertFalse(self.ctx.registry.is_enabled("technical"))


class TestAppWiring(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.ctx, self.auth, _ = make_ctx(self.d)

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_a_crashing_custom_page_is_isolated(self):
        def bad_page(st, ctx, session, spec):
            raise ValueError("page bug")
        self.ctx.registry.register(ModuleSpec("bad_page", "Bad", category="custom_screener", run=lambda c: {}, page=bad_page))
        st = FakeSt()
        appmod.render_page(st, self.ctx, VIEWER, "module:bad_page")
        self.assertIn("This page failed: ValueError: page bug", st.texts("error"))
        st2 = FakeSt()
        appmod.render_page(st2, self.ctx, VIEWER, "no_such_page")
        self.assertIn("This page failed", st2.texts("error"))

    def test_new_custom_module_end_to_end_without_touching_any_engine(self):
        self.assertNotIn("↳ Near 52-Week High", [n.label.strip() for n in self.ctx.registry.navigation()])
        self.ctx.registry.set_enabled("near_52w_high", True)                                # declared in config/modules.yaml, disabled by default
        nav = [n for n in self.ctx.registry.navigation() if n.module_id == "near_52w_high"]
        self.assertEqual(len(nav), 1)
        o = self.ctx.registry.run("near_52w_high", self.ctx)
        self.assertEqual((o.status, o.output_problems), ("ok", []))
        self.assertEqual(o.summary["stocks"], 5)
        st = FakeSt()
        appmod.render_page(st, self.ctx, VIEWER, nav[0].key)
        self.assertIn("Near 52-Week High", st.texts("title"))
        self.assertTrue(st.has("dataframe"))

    def test_login_box(self):
        st = FakeSt()
        self.assertIs(appmod.login_box(st, Authenticator("", "x", None)), VIEWER)
        self.assertIn("not configured", st.texts("caption"))
        auth = Authenticator("mechvec@gmail.com", "Sami", hash_password("a-long-password", iterations=1000))
        st = FakeSt(inputs={"login_email": "mechvec@gmail.com", "login_pw": "wrong"}, press={"login_btn"})
        self.assertIs(appmod.login_box(st, auth), VIEWER)
        self.assertIn("Wrong e-mail or password", st.texts("error"))
        st = FakeSt(inputs={"login_email": "mechvec@gmail.com", "login_pw": "a-long-password"}, press={"login_btn"})
        appmod.login_box(st, auth)
        self.assertTrue(st.session_state["session"].is_admin)
        self.assertTrue(st.has("rerun"))
        st.session_state["session"] = st.session_state["session"]
        self.assertTrue(appmod.login_box(st, auth).is_admin)
        for _ in range(5):
            s2 = FakeSt(inputs={"login_email": "mechvec@gmail.com", "login_pw": "bad"}, press={"login_btn"})
            appmod.login_box(s2, auth)
        # lockout lives in each browser session's state
        s3 = FakeSt(inputs={"login_email": "mechvec@gmail.com", "login_pw": "bad"}, press={"login_btn"})
        for _ in range(5):
            appmod.login_box(s3, auth)
        self.assertIn("Too many attempts", s3.texts("error"))

    def test_run_app_navigates_and_flags_a_test_universe(self):
        with mock.patch.object(appmod, "build_app", return_value=(self.ctx, self.auth)):
            st = FakeSt(inputs={"radio": "3️⃣ Screener 3 · Shariah Compliance Screener"})
            appmod.run_app(st)
        self.assertTrue(st.has("set_page_config"))
        self.assertIn("5 stocks", st.texts("caption"))
        self.assertIn("TEST universe in use", st.texts("warning"))
        self.assertIn("No Shariah results yet", st.texts("info"))                           # page for the chosen nav entry rendered


if __name__ == "__main__":
    unittest.main()
