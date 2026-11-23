"""Persistent job state (state/execution/jobs.json): one record per module per run, so a crashed run can be recognised and resumed."""
from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path

from ..core.io import atomic_write_text

KEEP = 400
QUEUED, RUNNING, SUCCEEDED, FAILED, INTERRUPTED, SKIPPED, NOT_CONFIGURED, DISABLED = (
    "queued", "running", "succeeded", "failed", "interrupted", "skipped", "not_configured", "disabled")
OUTCOME_TO_JOB = {"ok": SUCCEEDED, "error": FAILED, "unknown": FAILED, "invalid": FAILED, "not_configured": NOT_CONFIGURED, "disabled": DISABLED}


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class JobStore:
    def __init__(self, path: str | Path):
        self.path, self._lock = Path(path), threading.Lock()

    def _load(self) -> list:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []

    def _save(self, rows: list) -> None:
        atomic_write_text(self.path, json.dumps(rows[-KEEP:], indent=1, ensure_ascii=False) + "\n")

    def create_run(self, run_id: str, modules: list[str], trigger: str, now: datetime) -> None:
        with self._lock:
            rows = self._load() + [{"run_id": run_id, "module": m, "status": QUEUED, "attempt": 0, "trigger": trigger, "queued_at": _iso(now),
                                    "started_at": None, "finished_at": None, "error": None, "reason": None, "summary": {}} for m in modules]
            self._save(rows)

    def update(self, run_id: str, module: str, **fields) -> None:
        with self._lock:
            rows = self._load()
            for r in reversed(rows):
                if r["run_id"] == run_id and r["module"] == module:
                    r.update(fields)
                    break
            self._save(rows)

    def latest_run_id(self) -> str | None:
        rows = self._load()
        return rows[-1]["run_id"] if rows else None

    def latest_jobs(self) -> dict:
        rid = self.latest_run_id()
        return {r["module"]: r for r in self._load() if r["run_id"] == rid}

    def recover_interrupted(self, now: datetime) -> list[tuple]:
        """Called while holding the exclusive run lock: a job still 'running' belongs to a process that died."""
        out = []
        with self._lock:
            rows = self._load()
            for r in rows:
                if r["status"] == RUNNING:
                    r.update(status=INTERRUPTED, finished_at=_iso(now), error="the run was interrupted (the process ended before the job finished)")
                    out.append((r["run_id"], r["module"]))
            if out:
                self._save(rows)
        return out

    def to_resume(self) -> list[str]:
        return [m for m, r in self.latest_jobs().items() if r["status"] in (INTERRUPTED, QUEUED, SKIPPED)]

    def to_retry(self) -> list[str]:
        return [m for m, r in self.latest_jobs().items() if r["status"] in (FAILED, INTERRUPTED)]
