"""Streamlit entry point:   streamlit run src/mizan/dashboard/app.py      (after `pip install -e .`)

Absolute imports on purpose: Streamlit executes this file as a script, not as part of the package."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))       # .../src - lets a hosted app import `mizan` without a separate install step

from mizan.core.auth import VIEWER, authenticator_from_config
from mizan.core.config import load_config
from mizan.dashboard import pages
from mizan.dashboard.sync import maybe_sync
from mizan.modules.builtin import build_registry
from mizan.modules.context import RunContext
from mizan.modules.registry import resolve

CORE_PAGES = {"overview": pages.render_overview, "data_management": pages.render_data_management,
              "execution_monitor": pages.render_execution_monitor, "custom_screeners": pages.render_custom_screeners,
              "custom_analysis": pages.render_custom_analysis, "system_config": pages.render_system_configuration}
BUILTIN_PAGE_KEYS = ("technical", "fundamental", "shariah", "annual_report")


def load_modules_yaml(root) -> list:
    import yaml
    try:
        with open(root / "config" / "modules.yaml", encoding="utf-8") as fh:
            return (yaml.safe_load(fh) or {}).get("modules", [])
    except OSError:
        return []


def build_app(cfg: dict | None = None, env=None, ctx: RunContext | None = None):
    """(ctx with registry attached, authenticator). `ctx` can be injected (tests)."""
    cfg = cfg or load_config()
    ctx = ctx or RunContext.live(cfg)
    ctx.registry = build_registry(cfg, load_modules_yaml(ctx.root), state_path=ctx.store / "state" / "modules_state.json")
    return ctx, authenticator_from_config(cfg, env)


def page_for(key: str, ctx: RunContext):
    """-> (render function, module spec or None) for a navigation key."""
    if key in CORE_PAGES:
        return CORE_PAGES[key], None
    mid = key.split(":", 1)[1] if key.startswith("module:") else key
    spec = ctx.registry.get(mid)
    if spec is None:
        raise KeyError(f"unknown page {key!r}")
    return (resolve(spec.page) if spec.page else pages.render_generic_module), spec


def render_page(st, ctx: RunContext, session, key: str) -> None:
    """One page, isolated: a crash inside a page (a custom one included) shows an error box and never takes the app down."""
    try:
        fn, spec = page_for(key, ctx)
        fn(st, ctx, session, spec)
    except Exception as exc:
        st.error(f"This page failed: {type(exc).__name__}: {exc}")


def login_box(st, auth):
    """Sidebar: admin sign-in / sign-out. Everyone else stays a read-only viewer."""
    sess = st.session_state.get("session")
    if sess is not None and sess.is_admin:
        st.sidebar.success(f"Signed in as {sess.name} (admin)")
        if st.sidebar.button("Sign out"):
            st.session_state["session"] = None
            st.rerun()
        return sess
    if not auth.enabled:
        st.sidebar.caption("Admin login is not configured (no password set) - everyone is a read-only viewer. "
                           "To enable it, run  python -m mizan.core.auth hash --write-env  and restart the dashboard.")
        return VIEWER
    with st.sidebar.expander("🔐 Admin login"):
        email = st.text_input("E-mail", key="login_email")
        pw = st.text_input("Password", type="password", key="login_pw")
        if st.button("Sign in", key="login_btn"):
            s = auth.login(email, pw, st.session_state)
            if s:
                st.session_state["session"] = s
                st.rerun()
            elif auth.locked_until(st.session_state):
                st.error("Too many attempts. Try again in a few minutes.")
            else:
                st.error("Wrong e-mail or password.")
    return VIEWER


def run_app(st) -> None:
    st.set_page_config(page_title="Mizan Market Intelligence", page_icon="⚖️", layout="wide")

    @st.cache_resource
    def _boot():
        return build_app()
    ctx, auth = _boot()
    sync_status = maybe_sync(ctx.store)                       # hosted mode only: pull the data branch (cheap when fresh)
    st.sidebar.title("⚖️ Mizan")
    session = login_box(st, auth)
    nav = ctx.registry.navigation()
    label = st.sidebar.radio("Navigate", [n.label for n in nav])
    entry = next(n for n in nav if n.label == label)
    try:
        snap = ctx.universe_service.get()
        st.sidebar.caption(f"Universe: {snap.count} stocks ({ctx.universe_service.path.name})")
        if ctx.universe_service.path.name != "stock_universe.txt":
            st.sidebar.warning("TEST universe in use")
    except Exception as exc:
        st.sidebar.error(f"Universe problem: {exc}")
    if sync_status is not None:
        (st.sidebar.warning if sync_status.startswith("error") else st.sidebar.caption)(f"Data from the repository: {sync_status}")
    render_page(st, ctx, session, entry.key)


if __name__ == "__main__":
    import streamlit as _st
    run_app(_st)
