"""The admin login must be switchable-on by following the documented steps: .env is read, the hash command can write it, login works."""
import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
from dash_helpers import ROOT  # noqa: E402
from fake_st import FakeSt  # noqa: E402
from mizan.core import auth as authmod  # noqa: E402
from mizan.core.auth import authenticator_from_config, hash_password, verify_password  # noqa: E402
from mizan.core.config import load_config  # noqa: E402
from mizan.core.env import load_env, parse_env_text, set_env_value  # noqa: E402
from mizan.dashboard import app as appmod  # noqa: E402

HASH = "pbkdf2_sha256$600000$00ff$aa11"            # looks like a real hash: contains '$' characters


class TestEnvFile(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_parse(self):
        got = parse_env_text("\ufeff# comment\nA=1\nexport B=\"two words\"\nC='x y'\nD=plain  # trailing note\nE=\nBAD LINE\nH=" + HASH + "\n")
        self.assertEqual(got, {"A": "1", "B": "two words", "C": "x y", "D": "plain", "E": "", "H": HASH})   # '$' kept literally

    def test_load_env_never_overrides_the_real_environment(self):
        f = self.d / ".env"
        f.write_text("T_ONE=from_file\nT_TWO=from_file\nT_EMPTY=\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"T_TWO": "from_shell"}, clear=False):
            os.environ.pop("T_ONE", None); os.environ.pop("T_EMPTY", None)
            self.assertEqual(load_env(f), ["T_ONE"])
            self.assertEqual((os.environ["T_ONE"], os.environ["T_TWO"]), ("from_file", "from_shell"))
            self.assertNotIn("T_EMPTY", os.environ)
        self.assertEqual(load_env(self.d / "missing.env"), [])

    def test_set_env_value_replaces_adds_and_keeps_the_rest(self):
        f = self.d / ".env"
        set_env_value(f, "K", "v1")                                       # creates the file
        self.assertEqual(f.read_text(), "K=v1\n")
        f.write_text("# keep me\nGEMINI_API_KEY=abc\nexport K=old\n#K=commented\n", encoding="utf-8")
        set_env_value(f, "K", HASH)
        lines = f.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines, ["# keep me", "GEMINI_API_KEY=abc", f"K={HASH}", "#K=commented"])
        set_env_value(f, "NEW", "1")
        self.assertEqual(f.read_text(encoding="utf-8").splitlines()[-1], "NEW=1")
        self.assertEqual(list(self.d.glob(".w.*")), [])                   # no temp files left behind

    def test_load_config_reads_the_projects_env_file(self):
        (self.d / ".env").write_text("MIZAN_TEST_FROM_ENV_FILE=yes\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"MIZAN_HOME": str(self.d)}, clear=False):
            os.environ.pop("MIZAN_TEST_FROM_ENV_FILE", None)
            load_config(ROOT / "config")
            self.assertEqual(os.environ.get("MIZAN_TEST_FROM_ENV_FILE"), "yes")
            os.environ.pop("MIZAN_TEST_FROM_ENV_FILE", None)

    def test_env_file_is_git_ignored(self):
        self.assertIn(".env", (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines())


class TestWriteEnvCommand(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.cfg = {"admin": {"email": "mechvec@gmail.com", "name": "Sami", "password_hash_env": "MIZAN_ADMIN_PASSWORD_HASH"}}

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def run_cli(self, argv, answers):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"MIZAN_HOME": str(self.d)}, clear=False), mock.patch("getpass.getpass", side_effect=answers), contextlib.redirect_stdout(out):
            code = authmod._cli(argv)
        return code, out.getvalue()

    def test_write_env_then_login_works_end_to_end(self):
        code, out = self.run_cli(["hash", "--write-env"], ["a-long-password", "a-long-password"])
        self.assertEqual(code, 0)
        self.assertIn("Saved to", out)
        values = parse_env_text((self.d / ".env").read_text(encoding="utf-8"))
        self.assertTrue(verify_password("a-long-password", values["MIZAN_ADMIN_PASSWORD_HASH"]))
        self.assertNotIn("a-long-password", (self.d / ".env").read_text(encoding="utf-8"))          # only the hash is stored
        auth = authenticator_from_config(self.cfg, env=values)
        self.assertTrue(auth.enabled)
        self.assertTrue(auth.login("mechvec@gmail.com", "a-long-password", {}).is_admin)
        self.assertIsNone(auth.login("mechvec@gmail.com", "wrong-password", {}))

    def test_without_write_env_it_prints_the_exact_line(self):
        code, out = self.run_cli(["hash"], ["a-long-password", "a-long-password"])
        self.assertEqual(code, 0)
        self.assertIn("MIZAN_ADMIN_PASSWORD_HASH=pbkdf2_sha256$", out)
        self.assertFalse((self.d / ".env").exists())

    def test_weak_or_mismatched_passwords_are_refused_and_nothing_is_written(self):
        for answers in (["short", "short"], ["a-long-password", "different-password"]):
            code, out = self.run_cli(["hash", "--write-env"], answers)
            self.assertEqual(code, 1)
            self.assertFalse((self.d / ".env").exists())

    def test_usage_text_mentions_write_env(self):
        code, out = self.run_cli([], [])
        self.assertEqual(code, 2)
        self.assertIn("--write-env", out)


class TestSidebarGuidance(unittest.TestCase):
    def test_login_off_message_tells_how_to_turn_it_on(self):
        st = FakeSt()
        appmod.login_box(st, authmod.Authenticator("mechvec@gmail.com", "Sami", None))
        text = st.texts("caption")
        self.assertIn("not configured", text)
        self.assertIn("python -m mizan.core.auth hash --write-env", text)
        self.assertIn("restart", text)

    def test_login_form_appears_once_a_hash_exists(self):
        st = FakeSt()
        appmod.login_box(st, authmod.Authenticator("mechvec@gmail.com", "Sami", hash_password("a-long-password", iterations=1000)))
        self.assertTrue(any("Admin login" in str(a[0]) for a in st.args("expander")))
        self.assertIn("Sign in", st.labels())


if __name__ == "__main__":
    unittest.main()
