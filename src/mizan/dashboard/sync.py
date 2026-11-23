"""Hosted dashboard: keep a read-only copy of the `data` branch in the data store.

Only active when the environment variable MIZAN_DATA_REPO_URL is set (a secret on the hosting service). On your own PC it is not set, so your
local data_store is never touched. A folder that already has content and is not a git checkout is never overwritten. Nothing here raises:
on any problem the dashboard keeps showing the last data it has and the sidebar shows the message (the URL, which may contain a token, is
never part of a message)."""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path


def _git(args, cwd=None, runner=subprocess.run, timeout=180):
    return runner(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout)


def sync_data_branch(store: Path, url: str, branch: str = "data", max_age_minutes: float = 15, runner=subprocess.run, now=time.time) -> str:
    """-> 'cloned' | 'updated' | 'fresh' | 'skipped: ... not a clone ...' | 'error: ...'"""
    store = Path(store)
    marker = store / ".last_sync"
    last_line = lambda text: ((text or "").replace(url, "<repo>").strip().splitlines() or ["unknown error"])[-1]
    try:
        if (store / ".git").exists():
            if marker.exists() and now() - marker.stat().st_mtime < max_age_minutes * 60:
                return "fresh"
            r = _git(["fetch", "--quiet", "--depth", "1", "origin", branch], store, runner)
            if r.returncode == 0:
                r = _git(["reset", "--quiet", "--hard", "FETCH_HEAD"], store, runner)
            if r.returncode != 0:
                return "error: " + last_line(r.stderr)
            marker.write_text(str(now()))
            return "updated"
        if store.exists() and any(store.iterdir()):
            return "skipped: the data folder has content but is not a clone of the data branch (left untouched)"
        store.parent.mkdir(parents=True, exist_ok=True)
        r = _git(["clone", "--quiet", "--depth", "1", "--branch", branch, "--single-branch", url, str(store)], None, runner)
        if r.returncode != 0:
            return "error: " + last_line(r.stderr)
        marker.write_text(str(now()))
        return "cloned"
    except (OSError, subprocess.SubprocessError) as exc:
        return f"error: {type(exc).__name__}"


def maybe_sync(store: Path, env=None, **kw) -> str | None:
    """Hosted mode only: None when MIZAN_DATA_REPO_URL is not set (your own PC), otherwise the sync status."""
    env = os.environ if env is None else env
    url = env.get("MIZAN_DATA_REPO_URL")
    return sync_data_branch(store, url, env.get("MIZAN_DATA_BRANCH", "data"), **kw) if url else None


def data_as_of(store: Path) -> str | None:
    try:
        return json.loads((Path(store) / "published" / "v1" / "manifest.json").read_text(encoding="utf-8")).get("generated_at")
    except (OSError, ValueError):
        return None
