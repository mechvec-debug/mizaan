"""Go-live kit tests: run-time budget, execution manager, published contract, Execution Monitor, hosted sync, CI scripts, workflows, pre-push scan."""
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import dash_helpers as H  # noqa: E402
from dash_helpers import ADMIN, ROOT, make_ctx  # noqa: E402
from fake_st import FakeSt  # noqa: E402
from fakes import FakeClock, FakeProvider  # noqa: E402
from mizan.core.auth import VIEWER  # noqa: E402
from mizan.core.errors import ConfigError, ProviderPausedError  # noqa: E402
from mizan.dashboard import app as appmod  # noqa: E402
from mizan.dashboard.sync import maybe_sync, sync_data_branch  # noqa: E402
from mizan.data import DataManager  # noqa: E402
from mizan.data.locks import file_lock  # noqa: E402
from mizan.execution import ExecutionManager, JobStore, next_cron_run  # noqa: E402
from mizan.execution import __main__ as cli  # noqa: E402
from mizan.modules.registry import ModuleSpec, RunOutcome  # noqa: E402
from mizan.publish import PublishError, build_published, publish_all, validate_published  # noqa: E402
from mizan.publish import __main__ as pubcli  # noqa: E402

GIT = shutil.which("git")
BASH = shutil.which("bash") if sys.platform != "win32" else None


def git(*args, cwd=None):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout


# ------------------------------------------------------------------ run-time budget
class TestDeadline(unittest.TestCase):
    def setUp(self):
        self.d, self.clock, self.p = Path(tempfile.mkdtemp()), FakeClock(), FakeProvider()
        for n in ("TCS", "INFY", "WIPRO"):
            self.p.add(n, seed=1)
        self.dm = DataManager(self.p, self.d, {"throttle": {"requests_per_minute": 0, "max_retries": 0}}, clock=self.clock, sleep=lambda s: None)

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_no_new_download_after_the_deadline_but_cache_is_still_served(self):
        self.dm.get_ohlcv("TCS", years=1)
        self.dm.deadline = self.clock() + timedelta(minutes=10)
        self.dm.get_ohlcv("INFY", years=1)
        self.clock.advance(minutes=11)
        calls = len(self.p.calls)
        with self.assertRaises(ProviderPausedError) as cm:
            self.dm.get_ohlcv("WIPRO", years=1)
        self.assertIn("time budget", str(cm.exception))
        self.assertEqual(len(self.p.calls), calls)
        self.assertTrue(self.dm.get_ohlcv("TCS", years=1).from_cache)
        self.assertTrue(self.dm.stats()["deadline_reached"])

    def test_prefetch_defers_the_rest_and_no_deadline_means_no_limit(self):
        self.dm.deadline = self.clock() - timedelta(seconds=1)
        rep = self.dm.prefetch_ohlcv(["TCS", "INFY"], years=1)
        self.assertEqual((rep.downloaded, sorted(rep.deferred), self.p.calls), ([], ["INFY", "TCS"], []))
        self.dm.deadline = None
        self.assertFalse(self.dm.stats()["deadline_reached"])


# ------------------------------------------------------------------ execution manager
class MgrBase(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.ctx, _, self.prov = make_ctx(self.d, cfg_extra={"execution": {"schedule_cron_utc": "45 10 * * 1-5"}})
        self.reg, self.sleeps, self.calls = self.ctx.registry, [], {}
        self.mgr = ExecutionManager(self.reg, self.ctx, sleep=self.sleeps.append)

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def stub(self, mid, fn=None):
        def run(ctx):
            self.calls[mid] = self.calls.get(mid, 0) + 1
            return (fn(ctx, self.calls[mid]) if fn else {"done": 1}) or {}
        self.reg.register(ModuleSpec(mid, mid.title(), category="custom_screener", run=run))


class TestManager(MgrBase):
    def test_full_run_and_persistent_state(self):
        rep = self.mgr.run(trigger="test")
        self.assertEqual({m: v["status"] for m, v in rep.modules.items()},
                         {"annual_report": "not_configured", "technical": "ok", "fundamental": "ok", "shariah": "ok"})
        self.assertEqual(rep.exit_code, 0)                                            # an unconfigured module is a warning, not a failure
        self.assertTrue(any(a["module"] == "annual_report" and a["level"] == "warning" for a in rep.alerts))
        self.assertEqual([h["module_id"] for h in self.mgr.history.all()][-1], "shariah")      # evidence from the annual batch is read first
        fresh = JobStore(self.ctx.store / "state" / "execution" / "jobs.json")            # a new object = after a restart
        self.assertEqual(fresh.latest_jobs()["technical"]["status"], "succeeded")
        st = json.loads((self.ctx.store / "state" / "execution" / "status.json").read_text())
        self.assertEqual((st["run_id"], st["exit_code"]), (rep.run_id, 0))

    def test_failure_is_retried_isolated_and_reported(self):
        def boom(ctx, n):
            raise RuntimeError("module exploded")
        self.stub("boom_mod", boom); self.stub("ok_mod")
        rep = self.mgr.run(["boom_mod", "ok_mod"])
        self.assertEqual((self.calls, self.sleeps), ({"boom_mod": 2, "ok_mod": 1}, [60]))
        self.assertEqual((rep.modules["boom_mod"]["status"], rep.modules["ok_mod"]["status"], rep.exit_code), ("error", "ok", 1))
        self.assertTrue(any(a["level"] == "error" and "module exploded" in a["message"] for a in rep.alerts))

    def test_flaky_succeeds_on_retry_not_configured_never_retried(self):
        self.stub("flaky_mod", lambda c, n: (_ for _ in ()).throw(RuntimeError("t")) if n == 1 else {})
        def cfg(ctx, n):
            raise ConfigError("limits missing")
        self.stub("cfg_mod", cfg)
        rep = self.mgr.run(["flaky_mod", "cfg_mod"])
        self.assertEqual((self.calls, rep.exit_code), ({"flaky_mod": 2, "cfg_mod": 1}, 0))

    def test_retry_failed_runs_only_failed_modules(self):
        state = {"broken": True}
        self.stub("maybe_mod", lambda c, n: (_ for _ in ()).throw(RuntimeError("down")) if state["broken"] else {}); self.stub("ok_mod")
        self.mgr.run(["maybe_mod", "ok_mod"], retries=0)
        state["broken"] = False
        rep = self.mgr.run(retry_failed=True)
        self.assertEqual((rep.selected, self.calls["ok_mod"]), (["maybe_mod"], 1))

    def test_crash_mid_run_is_recovered_and_resumed(self):
        def hard_crash(ctx, n):
            if n == 1:
                raise KeyboardInterrupt()
        self.stub("a_mod"); self.stub("b_crash", hard_crash); self.stub("c_mod")
        with self.assertRaises(KeyboardInterrupt):
            self.mgr.run(["a_mod", "b_crash", "c_mod"])
        jobs = self.mgr.jobs.latest_jobs()
        self.assertEqual([jobs[m]["status"] for m in ("a_mod", "b_crash", "c_mod")], ["succeeded", "running", "queued"])
        self.assertFalse((self.ctx.store / "locks" / "execution.lock").exists())
        rep = self.mgr.run(resume=True)
        self.assertEqual((rep.selected, self.calls["a_mod"]), (["b_crash", "c_mod"], 1))
        self.assertTrue(any("interrupted" in a["message"] for a in rep.alerts))

    def test_lock_refuses_a_second_run_and_a_dead_runs_stale_lock_is_broken(self):
        self.stub("ok_mod")
        with file_lock(self.ctx.store / "locks" / "execution.lock"):
            rep = self.mgr.run(["ok_mod"])
        self.assertEqual((rep.locked, rep.exit_code, self.calls), (True, 2, {}))
        lock = self.ctx.store / "locks" / "execution.lock"
        lock.write_text("1")
        old = time.time() - 9 * 3600
        os.utime(lock, (old, old))
        self.assertEqual(self.mgr.run(["ok_mod"]).exit_code, 0)

    def test_time_budget_skips_remaining_modules_and_resume_finishes_them(self):
        self.stub("a_mod", lambda ctx, n: self.ctx.clock.advance(minutes=20)); self.stub("b_mod"); self.stub("c_mod")
        rep = self.mgr.run(["a_mod", "b_mod", "c_mod"], time_budget_minutes=10)
        self.assertEqual({m: v["status"] for m, v in rep.modules.items()}, {"a_mod": "ok", "b_mod": "skipped", "c_mod": "skipped"})
        self.assertTrue(rep.deadline_hit)
        self.assertEqual(rep.exit_code, 0)                                            # running out of time is not a failure
        self.assertIsNone(self.ctx.data_manager().deadline)
        rep2 = self.mgr.run(resume=True)
        self.assertEqual((rep2.selected, self.calls), (["b_mod", "c_mod"], {"a_mod": 1, "b_mod": 1, "c_mod": 1}))

    def test_out_of_time_with_no_results_is_a_warning(self):
        from mizan.execution.manager import RunReport
        rep = RunReport(run_id="r", trigger="t", started_at="x", deadline_hit=True)
        out = {"fundamental": RunOutcome("fundamental", "ok", summary={"status": "no_data"})}
        self.assertEqual([a["level"] for a in self.mgr._alerts(out, rep) if a["module"] == "fundamental"], ["warning"])
        rep.deadline_hit = False
        self.assertEqual([a["level"] for a in self.mgr._alerts(out, rep) if a["module"] == "fundamental"], ["error"])

    def test_selection_plan_cli_markdown_and_cron(self):
        self.assertEqual(self.mgr.select(["shariah", "technical", "nonexistent"]), ["technical", "shariah"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["plan", "--modules", "shariah,technical"]), 0)
        self.assertIn("['technical', 'shariah']", out.getvalue())
        self.stub("ok_mod")
        rep = self.mgr.run(["ok_mod"])
        self.assertIn(f"| ok_mod | ok |", cli._markdown(json.loads((self.ctx.store / "state" / "execution" / "status.json").read_text())))
        self.assertEqual(next_cron_run("45 10 * * 1-5", datetime(2026, 11, 13, 12, 0)), datetime(2026, 11, 16, 10, 45))     # Friday noon -> Monday
        self.assertEqual(next_cron_run("30 12 * * *", datetime(2026, 11, 14, 12, 30)), datetime(2026, 11, 15, 12, 30))


# ------------------------------------------------------------------ published contract
class TestPublish(MgrBase):
    def setUp(self):
        super().setUp()
        self.mgr.run(trigger="test")
        self.out = self.ctx.store / "published" / "v1"

    def test_build_validate_and_content(self):
        man = publish_all(self.ctx.store, self.ctx.universe())
        self.assertEqual(sorted(man["files"]), ["fundamental.json", "shariah.json", "status.json", "technical.json", "universe.json"])
        self.assertEqual(man["missing"], ["annual_report"])                                     # nothing invented for a module with no results
        self.assertEqual(validate_published(self.out), [])
        sh = json.loads((self.out / "shariah.json").read_text(encoding="utf-8"))
        self.assertEqual((sh["schema"], sh["count"], sh["kpis"]["total"], len(sh["rows"])), (1, 5, 5, 5))
        self.assertEqual(json.loads((self.out / "universe.json").read_text())["count"], 5)
        self.assertTrue((self.ctx.store / "published" / "_headers").read_text().startswith("/*"))
        self.assertIn("Access-Control-Allow-Origin", (self.ctx.store / "published" / "_headers").read_text())
        for name, meta in man["files"].items():
            self.assertEqual(hashlib.sha256((self.out / name).read_bytes()).hexdigest(), meta["sha256"])

    def test_tampering_is_detected_and_a_bad_set_is_refused(self):
        publish_all(self.ctx.store, self.ctx.universe())
        f = self.out / "shariah.json"
        f.write_text(f.read_text(encoding="utf-8").replace("COMPLIANT", "XOMPLIANT", 1), encoding="utf-8")
        self.assertIn("checksum", " ".join(validate_published(self.out)))
        (self.out / "technical.json").unlink()
        self.assertIn("missing", " ".join(validate_published(self.out)))
        (self.out / "manifest.json").unlink()
        self.assertEqual(len(validate_published(self.out)), 1)
        import mizan.publish.contract as c
        orig = c.validate_published
        c.validate_published = lambda d: ["forced problem"]
        try:
            with self.assertRaises(PublishError):
                publish_all(self.ctx.store, self.ctx.universe())
        finally:
            c.validate_published = orig

    def test_unchanged_results_give_byte_identical_files_one_row_per_line(self):
        a = build_published(self.ctx.store, self.ctx.universe(), now=datetime(2026, 11, 10, 9, 0))
        b = build_published(self.ctx.store, self.ctx.universe(), now=datetime(2026, 11, 10, 17, 30))
        same = [n for n in a["files"] if a["files"][n]["sha256"] == b["files"][n]["sha256"]]
        self.assertEqual(sorted(same), ["fundamental.json", "shariah.json", "technical.json", "universe.json"])   # only status/manifest carry the time
        lines = (self.out / "shariah.json").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 5 + 2)                                                    # header + 5 rows + closing line: git diffs stay tiny

    def test_a_module_whose_results_vanish_keeps_its_old_file_flagged_stale(self):
        publish_all(self.ctx.store, self.ctx.universe())
        shutil.rmtree(self.ctx.results_dir("technical"))
        man = build_published(self.ctx.store, self.ctx.universe())
        self.assertTrue(man["files"]["technical.json"]["stale"])
        self.assertTrue(man["modules"]["technical"]["stale"])
        self.assertEqual(validate_published(self.out), [])

    def test_publish_cli(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pubcli.main(["validate", "--store", str(self.ctx.store)]), 1)    # nothing published yet
            self.assertEqual(pubcli.main(["build", "--store", str(self.ctx.store)]), 0)
            self.assertEqual(pubcli.main(["validate", "--store", str(self.ctx.store)]), 0)


# ------------------------------------------------------------------ Execution Monitor page
class TestMonitorPage(MgrBase):
    def test_shows_last_run_next_run_and_published_health(self):
        self.mgr.run(trigger="test"); publish_all(self.ctx.store, self.ctx.universe())
        st = FakeSt(); appmod.render_page(st, self.ctx, VIEWER, "execution_monitor")
        text = st.texts()
        self.assertIn("Last run", text)
        self.assertIn("Next scheduled run:", text)
        self.assertIn("Published data:", text)
        self.assertIn("consistent", text)
        self.assertIn("not configured", text)                                              # the open annual-report limits surface as an alert
        self.assertFalse(any(b in st.labels() for b in ("🔁 Retry failed modules", "📤 Publish now")))

    def test_admin_buttons_run_retry_resume_and_publish(self):
        st = FakeSt(); appmod.render_page(st, self.ctx, ADMIN, "execution_monitor")
        for label in ("🔁 Retry failed modules", "⏩ Resume interrupted work", "📤 Publish now"):
            self.assertIn(label, st.labels())
        appmod.render_page(FakeSt(press={"run_all"}), self.ctx, ADMIN, "execution_monitor")
        self.assertEqual(json.loads((self.ctx.store / "state" / "execution" / "status.json").read_text())["trigger"], "dashboard")
        appmod.render_page(FakeSt(press={"publish_now"}), self.ctx, ADMIN, "execution_monitor")
        self.assertEqual(validate_published(self.ctx.store / "published" / "v1"), [])
        appmod.render_page(FakeSt(press={"run_all"}), self.ctx, VIEWER, "execution_monitor")      # a viewer cannot start anything
        self.assertEqual(len(self.mgr.jobs._load()), 4)


# ------------------------------------------------------------------ hosted dashboard sync + CI scripts (real local git)
@unittest.skipUnless(GIT, "git not installed")
class TestSyncAndScripts(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.origin = self.d / "origin.git"
        git("init", "--bare", "-b", "data", str(self.origin))

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def seed(self, name="seed", files=None):
        w = self.d / name
        git("clone", "--quiet", str(self.origin), str(w))
        git("checkout", "-B", "data", cwd=w)
        for rel, txt in (files or {"results/x.json": "[1]"}).items():
            (w / rel).parent.mkdir(parents=True, exist_ok=True); (w / rel).write_text(txt)
        git("add", "-A", cwd=w); git("commit", "--quiet", "-m", "seed", cwd=w); git("push", "--quiet", "origin", "data", cwd=w)
        return w

    def test_hosted_sync_clone_fresh_update_and_errors(self):
        w = self.seed()
        store = self.d / "ds"
        self.assertEqual(sync_data_branch(store, str(self.origin)), "cloned")
        self.assertEqual((store / "results" / "x.json").read_text(), "[1]")
        self.assertEqual(sync_data_branch(store, str(self.origin)), "fresh")
        (w / "results" / "y.json").write_text("[2]"); git("add", "-A", cwd=w); git("commit", "--quiet", "-m", "more", cwd=w); git("push", "--quiet", "origin", "data", cwd=w)
        self.assertEqual(sync_data_branch(store, str(self.origin), now=lambda: time.time() + 3600), "updated")
        self.assertTrue((store / "results" / "y.json").exists())
        secret = str(self.d / "SECRETTOKEN-missing.git")
        msg = sync_data_branch(self.d / "ds2", secret)
        self.assertTrue(msg.startswith("error:") and "SECRETTOKEN" not in msg)                 # never raises, never leaks the URL
        (self.d / "notgit").mkdir(); (self.d / "notgit" / "f").write_text("x")
        self.assertIn("not a clone", sync_data_branch(self.d / "notgit", str(self.origin)))
        self.assertIsNone(maybe_sync(self.d / "ds3", env={}))
        self.assertEqual(maybe_sync(self.d / "ds3", env={"MIZAN_DATA_REPO_URL": str(self.origin)}), "cloned")

    def run_script(self, name, ds, *args):
        env = {**os.environ, "DATA_REPO_URL": str(self.origin), "DATA_DIR": str(ds), "GIT_USER_NAME": "t", "GIT_USER_EMAIL": "t@t", "PUSH_RETRY_SECONDS": "0"}
        return subprocess.run(["bash", str(ROOT / "scripts" / "ci" / name), *args], env=env, capture_output=True, text=True)

    @unittest.skipUnless(BASH, "needs bash (Linux/macOS runner)")
    def test_ci_scripts_first_run_second_run_no_change_and_push_race(self):
        ds1 = self.d / "ds1"
        self.assertEqual(self.run_script("checkout_data_branch.sh", ds1).returncode, 0)             # branch does not exist yet
        (ds1 / "results").mkdir(); (ds1 / "results" / "a.json").write_text("[1]")
        (ds1 / "locks").mkdir(); (ds1 / "locks" / "x.lock").write_text("1")
        (ds1 / "tmp").mkdir(); (ds1 / "tmp" / "t.tmp").write_text("1")
        r = self.run_script("commit_data_branch.sh", ds1, "first")
        self.assertEqual(r.returncode, 0, r.stderr)
        files = git("--git-dir", str(self.origin), "ls-tree", "-r", "data", "--name-only").split()
        self.assertEqual(sorted(files), [".gitignore", "results/a.json"])                           # lock + temp files never committed
        ds2 = self.d / "ds2"
        self.assertEqual(self.run_script("checkout_data_branch.sh", ds2).returncode, 0)             # now it exists: cloned
        self.assertEqual((ds2 / "results" / "a.json").read_text(), "[1]")
        self.assertIn("no data changes", self.run_script("commit_data_branch.sh", ds2).stdout)
        # push race: two writers cloned the same state; the second push is rejected and must be replayed on top
        for n in ("w1", "w2"):
            self.run_script("checkout_data_branch.sh", self.d / n)
        (self.d / "w1" / "results" / "one.json").write_text("[1]"); (self.d / "w2" / "results" / "two.json").write_text("[2]")
        self.assertEqual(self.run_script("commit_data_branch.sh", self.d / "w1", "w1").returncode, 0)
        r = self.run_script("commit_data_branch.sh", self.d / "w2", "w2")
        self.assertEqual(r.returncode, 0, r.stderr)
        files = git("--git-dir", str(self.origin), "ls-tree", "-r", "data", "--name-only").split()
        self.assertTrue({"results/one.json", "results/two.json", "results/a.json"} <= set(files))   # nothing lost, no force-push


# ------------------------------------------------------------------ workflows
class TestWorkflows(unittest.TestCase):
    def load(self):
        import yaml
        return {p.name: (p.read_text(encoding="utf-8"), yaml.safe_load(p.read_text(encoding="utf-8"))) for p in sorted((ROOT / ".github" / "workflows").glob("*.yml"))}

    def test_structure_and_safety(self):
        wf = self.load()
        self.assertEqual(set(wf), {"daily_pipeline.yml", "annual_report_batch.yml", "data_maintenance.yml"})
        for name, (raw, d) in wf.items():
            self.assertEqual(d["concurrency"], {"group": "mizan-data", "cancel-in-progress": False}, name)       # one writer of the data branch
            self.assertEqual(d["permissions"], {"contents": "write"}, name)
            job = next(iter(d["jobs"].values()))
            self.assertLessEqual(job["timeout-minutes"], 360, name)
            self.assertNotRegex(raw, r"(ghp_|AIza|BEGIN PRIVATE KEY)", name)
            for step in job["steps"]:
                self.assertNotIn("${{ inputs.", step.get("run", ""), f"{name}: inputs must go through env (script injection)")
        raw, d = wf["daily_pipeline.yml"]
        on = d.get("on") or d.get(True)
        self.assertEqual(on["schedule"][0]["cron"], "45 10 * * 1-5")
        steps = {s.get("name"): s for s in d["jobs"]["pipeline"]["steps"] if s.get("name")}
        self.assertTrue(steps["Run the modules"]["continue-on-error"])
        self.assertEqual(steps["Save the data to the data branch"]["if"], "always()")                   # data saved even when a module failed
        self.assertIn("steps.run.outcome == 'failure'", steps["Fail the job if a module failed"]["if"])
        self.assertLess(int(on["workflow_dispatch"]["inputs"]["time_budget_minutes"]["default"]), d["jobs"]["pipeline"]["timeout-minutes"])

    def test_every_command_and_script_a_workflow_calls_exists(self):
        for name, (raw, d) in self.load().items():
            for mod in set(re.findall(r"python -m (mizan[\w.]*)", raw)):
                path = ROOT / "src" / Path(*mod.split(".")) / "__main__.py"
                self.assertTrue(path.exists() or (ROOT / "src" / Path(*mod.split(".")).with_suffix(".py")).exists(), f"{name}: {mod}")
            for script in re.findall(r"bash (scripts/ci/[\w.]+)", raw):
                self.assertTrue((ROOT / script).exists(), f"{name}: {script}")
            for req in re.findall(r"-r (requirements[\w.-]*)", raw):
                self.assertTrue((ROOT / req).exists())

    def test_ci_requirements_need_no_streamlit(self):
        txt = (ROOT / "requirements-ci.txt").read_text()
        self.assertNotIn("streamlit", txt)
        self.assertIn("yfinance", txt)


# ------------------------------------------------------------------ delta manifest ordering
class TestManifestOrder(unittest.TestCase):
    def test_phases_sort_by_number_not_alphabetically(self):
        import importlib.util
        names = ["phase9c_manifest.json", "phase11_manifest.json", "phase7_manifest.json", "phase9_manifest.json", "phase9b_manifest.json", "phase10_manifest.json"]
        for script in ("doctor.py", "verify_delta.py"):
            spec = importlib.util.spec_from_file_location(script[:-3], ROOT / "scripts" / script)
            mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
            self.assertEqual(sorted(names, key=mod.manifest_key), ["phase7_manifest.json", "phase9_manifest.json", "phase9b_manifest.json",
                                                                   "phase9c_manifest.json", "phase10_manifest.json", "phase11_manifest.json"])


# ------------------------------------------------------------------ pre-push scan
@unittest.skipUnless(GIT, "git not installed")
class TestPrepush(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        git("init", "--quiet", cwd=self.d)
        shutil.copy(ROOT / "scripts" / "prepush_check.py", self.d / "prepush_check.py")
        (self.d / "scripts").mkdir()
        shutil.move(str(self.d / "prepush_check.py"), str(self.d / "scripts" / "prepush_check.py"))

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def scan(self):
        r = subprocess.run([sys.executable, str(self.d / "scripts" / "prepush_check.py")], capture_output=True, text=True)
        return r.returncode, r.stdout

    def test_clean_repo_passes_and_secrets_are_caught(self):
        (self.d / "app.py").write_text("print('hello')\n")
        (self.d / ".gitignore").write_text(".env\ndata_store/\n")
        (self.d / ".env").write_text("X=1\n"); (self.d / "data_store").mkdir(); (self.d / "data_store" / "f.json").write_text("{}")
        self.assertEqual(self.scan()[0], 0)                                                          # .env and data_store are ignored: fine
        fake_google = "AI" + "za" + "A" * 35
        (self.d / "bad.py").write_text(f'KEY = "{fake_google}"\npassword = "hunter2hunter2"\n')
        (self.d / "key.json").write_text('{"private' + '_key": "x"}')
        code, out = self.scan()
        self.assertEqual(code, 1)
        for what in ("Google API key", "hard-coded password", "service-account JSON"):
            self.assertIn(what, out)
        self.assertNotIn(fake_google, out)                                                           # the report never prints the secret itself

    def test_an_empty_assignment_followed_by_another_line_is_not_a_secret(self):
        (self.d / ".env.example").write_text("GEMINI_API_KEY=\nFIREBASE_SERVICE_ACCOUNT=\nMIZAN_ADMIN_PASSWORD_HASH=\n")     # the real template
        self.assertEqual(self.scan()[0], 0)

    def test_committing_env_or_data_store_is_flagged(self):
        (self.d / ".env").write_text("X=1\n"); (self.d / "data_store").mkdir(); (self.d / "data_store" / "f.json").write_text("{}")
        code, out = self.scan()
        self.assertEqual(code, 1)
        self.assertIn(".env file would be committed", out)
        self.assertIn("data_store folder would be committed", out)


if __name__ == "__main__":
    unittest.main()
