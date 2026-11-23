"""Phase 9 tests (1/2): login + permissions, module registry (validation, isolation, navigation), runner + history."""
import json
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from dash_helpers import ROOT, make_ctx  # noqa: E402,F401
from mizan.core.auth import (Authenticator, Session, VIEWER, authenticator_from_config, can, hash_password, verify_password)  # noqa: E402
from mizan.core.errors import ConfigError  # noqa: E402
from mizan.execution import HistoryStore, run_modules  # noqa: E402
from mizan.modules.builtin import build_registry  # noqa: E402
from mizan.modules.context import RunContext  # noqa: E402
from mizan.modules.registry import ModuleSpec, Registry, validate_spec  # noqa: E402

FAST = 1000          # PBKDF2 iterations in tests only (production default is 600,000)


class T(datetime):
    pass


class TestAuth(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc)
        self.auth = Authenticator("Mechvec@Gmail.com", "Sami", hash_password("a-long-password", iterations=FAST), clock=lambda: self.now)

    def test_hash_is_salted_verifiable_and_never_the_password(self):
        a, b = hash_password("secret-password", iterations=FAST), hash_password("secret-password", iterations=FAST)
        self.assertNotEqual(a, b)                                            # random salt
        self.assertNotIn("secret-password", a)
        self.assertTrue(verify_password("secret-password", a) and not verify_password("Secret-password", a))
        for bad in (None, "", "plain", "md5$1$ab$cd", "pbkdf2_sha256$x$y$z"):
            self.assertFalse(verify_password("secret-password", bad))

    def test_admin_login_and_roles(self):
        st = {}
        s = self.auth.login("mechvec@gmail.com", "a-long-password", st)
        self.assertEqual((s.role, s.name, s.is_admin), ("admin", "Sami", True))
        self.assertTrue(all(can(s, p) for p in ("run_modules", "edit_universe", "edit_evidence", "toggle_modules")))
        self.assertEqual([can(VIEWER, p) for p in ("view_results", "run_modules", "edit_universe", "edit_evidence")], [True, False, False, False])
        self.assertFalse(can(None, "run_modules"))

    def test_wrong_credentials(self):
        st = {}
        self.assertIsNone(self.auth.login("mechvec@gmail.com", "wrong", st))
        self.assertIsNone(self.auth.login("someone@else.com", "a-long-password", st))     # right password, wrong e-mail
        self.assertEqual(st["auth_failures"], 2)

    def test_lockout_after_five_failures_then_expires(self):
        st = {}
        for _ in range(5):
            self.auth.login("mechvec@gmail.com", "nope", st)
        self.assertIsNotNone(self.auth.locked_until(st))
        self.assertIsNone(self.auth.login("mechvec@gmail.com", "a-long-password", st))    # even the right password is refused while locked
        self.now += timedelta(minutes=16)
        self.assertIsNotNone(self.auth.login("mechvec@gmail.com", "a-long-password", st))

    def test_fails_closed_without_a_configured_hash(self):
        a = authenticator_from_config({"admin": {"email": "mechvec@gmail.com", "name": "Sami", "password_hash_env": "MIZAN_X"}}, env={})
        self.assertFalse(a.enabled)
        self.assertIsNone(a.login("mechvec@gmail.com", "anything", {}))
        b = authenticator_from_config({"admin": {"email": "mechvec@gmail.com", "password_hash_env": "MIZAN_X"}}, env={"MIZAN_X": hash_password("pw-pw-pw-pw", iterations=FAST)})
        self.assertTrue(b.enabled)

    def test_no_password_or_hash_in_the_repo_config(self):
        text = (ROOT / "config" / "app.yaml").read_text(encoding="utf-8")
        self.assertNotIn("pbkdf2", text)
        self.assertIn("MIZAN_ADMIN_PASSWORD_HASH", text)


def ok_run(ctx):
    return {"done": 1}


def boom_run(ctx):
    raise RuntimeError("module exploded")


def cfg_run(ctx):
    raise ConfigError("limits missing")


class TestRegistry(unittest.TestCase):
    def test_validation_rules(self):
        self.assertEqual(validate_spec(ModuleSpec("good_one", "Good", run=ok_run)), [])
        probs = validate_spec(ModuleSpec("Bad Id", "", category="nope", run=None, universe_filter="x"))
        self.assertGreaterEqual(len(probs), 5)
        self.assertTrue(any("cannot be loaded" in p for p in validate_spec(ModuleSpec("x1", "X", run="no.such.module:fn"))))
        self.assertTrue(any("results_file" in p for p in validate_spec(ModuleSpec("x1", "X", run=ok_run, output_schema={"a": 1}))))

    def test_invalid_module_is_not_registered_and_is_remembered(self):
        r = Registry()
        self.assertTrue(r.register(ModuleSpec("Bad", "B", run=ok_run)))
        self.assertIsNone(r.get("Bad"))
        self.assertIn("Bad", r.invalid)
        self.assertEqual(r.register(ModuleSpec("dup_a", "A", run=ok_run)), [])
        self.assertTrue(r.register(ModuleSpec("dup_a", "A2", run=ok_run)))                    # duplicate id refused
        self.assertEqual(r.get("dup_a").name, "A")

    def test_navigation_has_the_ten_required_pages_in_order(self):
        keys = [n.key for n in build_registry({}).navigation()]
        self.assertEqual(keys, ["overview", "technical", "fundamental", "shariah", "annual_report", "data_management", "execution_monitor",
                                "custom_screeners", "custom_analysis", "system_config"])

    def test_new_module_appears_in_navigation_only_when_enabled_and_valid(self):
        r = build_registry({})
        r.register(ModuleSpec("my_screen", "My Screen", category="custom_screener", run=ok_run, enabled=False))
        r.register(ModuleSpec("my_analysis", "My Analysis", category="custom_analysis", run=ok_run))
        labels = [n.label.strip() for n in r.navigation()]
        self.assertNotIn("↳ My Screen", labels)
        self.assertIn("↳ My Analysis", labels)
        self.assertEqual([n.key for n in r.navigation()].index("module:my_analysis"), [n.key for n in r.navigation()].index("custom_analysis") + 1)
        r.set_enabled("my_screen", True)
        self.assertIn("↳ My Screen", [n.label.strip() for n in r.navigation()])

    def test_enable_state_persists_across_restarts(self):
        d = Path(tempfile.mkdtemp())
        try:
            r = build_registry({}, state_path=d / "s.json")
            r.set_enabled("technical", False)
            self.assertFalse(build_registry({}, state_path=d / "s.json").is_enabled("technical"))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_modules_yaml_declares_a_custom_module_without_touching_engines(self):
        r = build_registry({}, [{"id": "near_52w_high", "display_name": "Near High", "category": "custom_screener",
                                 "entrypoint": "mizan.screeners.custom.near_52w_high:run", "results_file": "results.json",
                                 "required_columns": ["ticker"], "enabled": True},
                                {"id": "broken", "display_name": "Broken", "entrypoint": "no.such:fn"},
                                {"id": "technical", "display_name": "Renamed Radar", "enabled": False}])
        self.assertIsNotNone(r.get("near_52w_high"))
        self.assertIn("broken", r.invalid)
        self.assertEqual((r.get("technical").name, r.is_enabled("technical")), ("Renamed Radar", False))
        self.assertIn("Renamed Radar", [n.label for n in r.navigation()][1])


class TestIsolationAndRunner(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.ctx, _, self.prov = make_ctx(self.d)
        self.reg = self.ctx.registry
        self.reg.register(ModuleSpec("ok_mod", "OK", category="custom_screener", run=ok_run))
        self.reg.register(ModuleSpec("boom_mod", "Boom", category="custom_screener", run=boom_run))
        self.reg.register(ModuleSpec("cfg_mod", "Cfg", category="custom_screener", run=cfg_run))

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_a_crashing_module_never_raises_and_never_blocks_the_others(self):
        outs = run_modules(self.reg, self.ctx, ["boom_mod", "ok_mod", "cfg_mod"])
        self.assertEqual([o.status for o in outs], ["error", "ok", "not_configured"])
        self.assertIn("module exploded", outs[0].error)
        self.assertIn("limits missing", outs[2].error)

    def test_disabled_and_unknown_modules(self):
        self.reg.set_enabled("ok_mod", False)
        self.assertEqual(self.reg.run("ok_mod", self.ctx).status, "disabled")
        self.assertEqual(self.reg.run("nothing", self.ctx).status, "unknown")

    def test_history_is_persisted_and_queryable(self):
        run_modules(self.reg, self.ctx, ["ok_mod", "boom_mod"], run_id="r1")
        run_modules(self.reg, self.ctx, ["ok_mod"], run_id="r2")
        h = HistoryStore(self.ctx.store / "state" / "execution" / "history.json")
        self.assertEqual([x["run_id"] for x in h.all()], ["r1", "r1", "r2"])
        self.assertEqual(h.recent(1)[0]["run_id"], "r2")
        self.assertEqual(h.last_by_module()["boom_mod"]["status"], "error")

    def test_output_schema_problems_are_reported_not_hidden(self):
        def writes_bad(ctx):
            p = ctx.results_dir("bad_out"); p.mkdir(parents=True, exist_ok=True)
            (p / "r.json").write_text(json.dumps([{"ticker": "X"}]))
        self.reg.register(ModuleSpec("bad_out", "Bad out", category="custom_screener", run=writes_bad,
                                     output_schema={"results_file": "r.json", "required_columns": ["ticker", "score"]}))
        o = self.reg.run("bad_out", self.ctx)
        self.assertEqual(o.status, "ok")
        self.assertIn("score", o.output_problems[0])
        self.reg.register(ModuleSpec("no_out", "No out", category="custom_screener", run=ok_run, output_schema={"results_file": "missing.json"}))
        self.assertIn("was not produced", self.reg.run("no_out", self.ctx).output_problems[0])

    def test_universe_filter_limits_a_module_to_shariah_compliant_stocks(self):
        run_modules(self.reg, self.ctx, ["shariah"])
        self.reg.get("ok_mod").universe_filter = "shariah_compliant"
        self.ctx.registry = self.reg
        self.assertEqual(self.ctx.tickers("ok_mod"), [])                       # nobody is proven compliant yet (C1 evidence pending)
        self.ctx.evidence().set("CCC", "haram_business_revenue", 0.0, source="manual", fy_end="2026-03-31", evidence="note")
        self.ctx.evidence().set("CCC", "non_compliant_investments", 20.0, source="manual", fy_end="2026-03-31", evidence="note")
        run_modules(self.reg, self.ctx, ["shariah"])
        self.assertEqual(self.ctx.tickers("ok_mod"), ["CCC"])
        self.assertEqual(self.ctx.tickers("technical"), ["AAA", "BBB", "CCC", "BANK", "DDD"])   # others unaffected

    def test_universe_filter_without_shariah_results_is_a_clear_configuration_message(self):
        self.reg.get("ok_mod").universe_filter = "shariah_compliant"
        self.assertEqual(self.reg.run("ok_mod", self.ctx).status, "ok")         # ok_run does not ask for tickers
        def wants(ctx):
            return {"n": len(ctx.tickers("ok_mod"))}
        self.reg.register(ModuleSpec("wants_t", "W", category="custom_screener", run=wants, universe_filter="shariah_compliant"))
        o = self.reg.run("wants_t", self.ctx)
        self.assertEqual(o.status, "not_configured")
        self.assertIn("Shariah", o.error)


if __name__ == "__main__":
    unittest.main()
