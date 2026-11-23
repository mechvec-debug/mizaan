"""Admin authentication and permissions.

* End users need no login and are READ-ONLY ("viewer").
* The admin (Sami, mechvec@gmail.com) logs in with e-mail + password. The password is NEVER stored in code or config: only a
  salted PBKDF2-HMAC-SHA256 hash, supplied through the environment variable named in config/app.yaml (admin.password_hash_env,
  default MIZAN_ADMIN_PASSWORD_HASH). Create the hash on your own machine:   python -m mizan.core.auth hash
* If no hash is configured, admin login is simply disabled (everyone is a viewer) - it fails closed.
* Wrong attempts are counted in the caller's session state; after 5 failures the login is locked for 15 minutes.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

ITERATIONS = 600_000
PERMISSIONS = {"view_results", "run_modules", "edit_universe", "edit_evidence", "toggle_modules", "view_config"}
ROLES = {"viewer": {"view_results"}, "admin": set(PERMISSIONS)}
_DUMMY = None


def hash_password(password: str, salt: bytes | None = None, iterations: int = ITERATIONS) -> str:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    try:
        algo, it, salt, want = (stored or "").split("$")
        if algo != "pbkdf2_sha256":
            return False
        got = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), int(it)).hex()
        return hmac.compare_digest(got, want)
    except (ValueError, TypeError):
        return False


@dataclass(frozen=True)
class Session:
    email: str | None
    name: str
    role: str

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


VIEWER = Session(None, "Guest", "viewer")


def can(session: Session | None, permission: str) -> bool:
    return permission in ROLES.get((session or VIEWER).role, set())


class Authenticator:
    def __init__(self, admin_email: str, admin_name: str, password_hash: str | None, max_failures: int = 5, lockout_minutes: int = 15,
                 clock: Callable[[], datetime] | None = None, iterations_for_dummy: int = 1000):
        self.email, self.name, self.hash = (admin_email or "").strip().lower(), admin_name or "Admin", password_hash
        self.max_failures, self.lockout = max_failures, timedelta(minutes=lockout_minutes)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._dummy = hash_password("x", b"0" * 16, iterations_for_dummy)

    @property
    def enabled(self) -> bool:
        return bool(self.hash) and bool(self.email)

    def locked_until(self, state: dict) -> datetime | None:
        until = state.get("auth_locked_until")
        return until if until and until > self.clock() else None

    def login(self, email: str, password: str, state: dict) -> Session | None:
        """state = the caller's mutable session dict (st.session_state). Returns the admin Session or None."""
        if not self.enabled or self.locked_until(state):
            return None
        same = hmac.compare_digest((email or "").strip().lower().encode(), self.email.encode())
        ok = verify_password(password or "", self.hash if same else self._dummy)       # same work either way: no e-mail probing
        if same and ok:
            state["auth_failures"], state["auth_locked_until"] = 0, None
            return Session(self.email, self.name, "admin")
        state["auth_failures"] = state.get("auth_failures", 0) + 1
        if state["auth_failures"] >= self.max_failures:
            state["auth_locked_until"], state["auth_failures"] = self.clock() + self.lockout, 0
        return None


def authenticator_from_config(cfg: dict, env=None) -> Authenticator:
    a = (cfg or {}).get("admin", {}) or {}
    env = os.environ if env is None else env
    return Authenticator(a.get("email", ""), a.get("name", "Admin"), env.get(a.get("password_hash_env", "MIZAN_ADMIN_PASSWORD_HASH")))


def _cli(argv) -> int:
    if argv[:1] != ["hash"]:
        print("usage: python -m mizan.core.auth hash [--write-env]\n"
              "  prompts for the admin password and prints its salted hash.\n"
              "  --write-env stores it in the project's .env as MIZAN_ADMIN_PASSWORD_HASH (then restart the dashboard).")
        return 2
    import getpass
    p1, p2 = getpass.getpass("New admin password: "), getpass.getpass("Repeat: ")
    if p1 != p2 or len(p1) < 10:
        print("Passwords differ or are shorter than 10 characters.")
        return 1
    h = hash_password(p1)
    if "--write-env" in argv:
        from .env import set_env_value
        from .paths import project_root
        path = set_env_value(project_root() / ".env", "MIZAN_ADMIN_PASSWORD_HASH", h)
        print(f"\nSaved to {path}\nRestart the dashboard, then sign in from the sidebar (admin login).")
    else:
        print("\nAdd this line to the project's .env file (or set it as a secret when hosted):\n\nMIZAN_ADMIN_PASSWORD_HASH=" + h +
              "\n\nTip: `python -m mizan.core.auth hash --write-env` does it for you.")
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
