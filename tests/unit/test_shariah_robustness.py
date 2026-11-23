"""An optional data source must never take down the primary one; failure reasons must be visible to the user."""
import contextlib
import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import dash_helpers as H  # noqa: E402
from mizan.core.errors import PermanentProviderError  # noqa: E402


class Exploding:
    """A BSE adapter that fails in a way nobody anticipated (not a normal provider error)."""
    name = "bsefake"

    def __init__(self, exc):
        self.exc = exc

    def provider_symbol(self, t):
        return t

    def fetch_xbrl_facts(self, code):
        raise self.exc


class TestOptionalSourceIsolation(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        H.DemoProvider.S["TCS"] = H._st(H.GOOD, H.PNL)                        # TCS has a seed BSE code, AAA does not
        self.ctx, _, _ = H.make_ctx(self.d, names=("TCS", "AAA"))

    def tearDown(self):
        H.DemoProvider.S.pop("TCS", None)
        shutil.rmtree(self.d, ignore_errors=True)

    def records(self):
        data = json.loads((self.ctx.results_dir("shariah") / "latest_results.json").read_text(encoding="utf-8"))["data"]
        return {r["ticker"]: r for r in data}

    def test_unexpected_error_in_the_xbrl_path_does_not_fail_the_stock(self):
        for exc in (KeyError("xbrl"), AttributeError("no attribute"), ValueError("bad"), RuntimeError("boom"), TypeError("x")):
            shutil.rmtree(self.ctx.store / "results", ignore_errors=True)
            self.ctx.xbrl_provider_factory = lambda e=exc: Exploding(e)
            o = self.ctx.registry.run("shariah", self.ctx)
            self.assertEqual(o.status, "ok", repr(exc))
            self.assertEqual(o.summary["failed"], 0, repr(exc))               # before the fix the BSE-mapped stock failed
            r = self.records()
            self.assertEqual(sorted(r), ["AAA", "TCS"])
            self.assertEqual(r["TCS"]["data_source"], "yahoo")                # fell back to the primary source
            notes = json.loads((self.ctx.results_dir("shariah") / "run_report.json").read_text())["data_notes"]
            self.assertIn(f"XBRL error ({type(exc).__name__}", notes["TCS"][0])   # and the reason is recorded, not hidden

    def test_a_normal_provider_failure_is_still_a_clean_note(self):
        self.ctx.xbrl_provider_factory = lambda: Exploding(PermanentProviderError("BSE says no"))
        o = self.ctx.registry.run("shariah", self.ctx)
        self.assertEqual((o.status, o.summary["failed"]), ("ok", 0))
        notes = json.loads((self.ctx.results_dir("shariah") / "run_report.json").read_text())["data_notes"]
        self.assertIn("XBRL unavailable", notes["TCS"][0])

    def test_test_script_prints_the_reasons_not_just_counts(self):
        self.ctx.xbrl_provider_factory = lambda: Exploding(KeyError("xbrl"))
        self.ctx.registry.run("shariah", self.ctx)
        spec = importlib.util.spec_from_file_location("run_test_batch", H.ROOT / "scripts" / "run_test_batch.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            mod.print_reasons(self.ctx, ["shariah"])
        out = buf.getvalue()
        self.assertIn("[shariah] notes:", out)
        self.assertIn("TCS", out)
        self.assertIn("KeyError", out)


class TestDoctorScript(unittest.TestCase):
    def test_doctor_runs_and_reports_every_section(self):
        r = subprocess.run([sys.executable, str(H.ROOT / "scripts" / "doctor.py")], capture_output=True, text=True, cwd=str(H.ROOT))
        self.assertIn(r.returncode, (0, 1))
        for section in ("== 1. Python", "== 2. Packages", "== 3. Streamlit", "== 4. config/app.yaml", "== 5. Delta files", "== 6. Reasons"):
            self.assertIn(section, r.stdout)
        self.assertIn("RESULT:", r.stdout)


if __name__ == "__main__":
    unittest.main()
