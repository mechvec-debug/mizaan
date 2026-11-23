"""Execution manager: runs modules one after another, each isolated, with retries, a run-time budget, a run lock, resume / retry-failed,
persistent job state and a status file + alerts + exit code for the dashboard and GitHub Actions.

* a crash in one module never stops the others; a crashed PROCESS is recognised next time (job left 'running') and resumed
* the run-time budget stops NEW work (downloads and modules) before the CI job limit; the cache and each module's own queue make the
  next run continue exactly where this one stopped - nothing is lost or redone
* one run per data store at a time (lock file); identical downloads are prevented by the Data Manager's per-key locks
Parallel execution is not part of this release (sequential is what GitHub Actions needs and keeps provider rate limits predictable)."""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

from ..core.io import atomic_write_text
from ..data.locks import FileLockTimeout, file_lock
from .history import HistoryStore, _slim
from .jobs import FAILED, JobStore, OUTCOME_TO_JOB, RUNNING, SKIPPED

DEFAULTS = {"order": ["annual_report", "technical", "fundamental", "shariah"], "retries": 1, "retry_delay_seconds": 60,
            "time_budget_minutes": None, "lock_stale_minutes": 480}


@dataclass
class RunReport:
    run_id: str
    trigger: str
    started_at: str
    finished_at: str = ""
    exit_code: int = 0
    selected: list = field(default_factory=list)
    modules: dict = field(default_factory=dict)
    alerts: list = field(default_factory=list)
    deadline_hit: bool = False
    locked: bool = False
    time_budget_minutes: float | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class ExecutionManager:
    def __init__(self, registry, ctx, *, jobs: JobStore | None = None, history: HistoryStore | None = None, sleep=time.sleep):
        self.registry, self.ctx, self.sleep = registry, ctx, sleep
        base = ctx.store / "state" / "execution"
        self.jobs, self.history = jobs or JobStore(base / "jobs.json"), history or HistoryStore(base / "history.json")
        self.status_path = base / "status.json"
        self.cfg = {**DEFAULTS, **((ctx.cfg or {}).get("execution") or {})}
        ctx.registry = registry

    def select(self, modules: list[str] | None = None, resume: bool = False, retry_failed: bool = False) -> list[str]:
        enabled = [s.id for s in self.registry.enabled()]
        order = [m for m in self.cfg["order"] if m in enabled] + [m for m in enabled if m not in self.cfg["order"]]
        if modules:
            want = set(modules)
        elif resume or retry_failed:
            want = set((self.jobs.to_resume() if resume else []) + (self.jobs.to_retry() if retry_failed else []))
        else:
            want = set(order)
        return [m for m in order if m in want]

    def run(self, modules: list[str] | None = None, *, retries: int | None = None, time_budget_minutes: float | None = None,
            trigger: str = "manual", resume: bool = False, retry_failed: bool = False) -> RunReport:
        now = self.ctx.now()
        rep = RunReport(run_id=now.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6], trigger=trigger, started_at=_iso(now))
        try:
            with file_lock(self.ctx.store / "locks" / "execution.lock", timeout=0.0, stale_after=self.cfg["lock_stale_minutes"] * 60):
                return self._run_locked(rep, modules, self.cfg["retries"] if retries is None else retries,
                                        time_budget_minutes if time_budget_minutes is not None else self.cfg["time_budget_minutes"], resume, retry_failed)
        except FileLockTimeout:
            rep.locked, rep.exit_code, rep.finished_at = True, 2, _iso(self.ctx.now())
            rep.alerts.append({"level": "error", "module": "*", "message": "another run is already in progress on this data store (lock held) - nothing was started"})
            return rep

    def _run_locked(self, rep, modules, retries, budget, resume, retry_failed) -> RunReport:
        now = self.ctx.now()
        for rid, mid in self.jobs.recover_interrupted(now):
            rep.alerts.append({"level": "warning", "module": mid, "message": f"a previous run ({rid}) was interrupted while running this module; it is picked up again now"})
        ids = self.select(modules, resume, retry_failed)
        rep.selected, rep.time_budget_minutes = ids, budget
        if not ids:
            rep.alerts.append({"level": "info", "module": "*", "message": "nothing to run (no enabled module matched the selection)"})
            return self._finish(rep, {})
        self.jobs.create_run(rep.run_id, ids, rep.trigger, now)
        deadline = now + timedelta(minutes=budget) if budget else None
        dm = None
        if deadline:
            dm = self.ctx.data_manager()
            dm.deadline = deadline
        outcomes: dict = {}
        try:
            for mid in ids:
                outcomes[mid] = self._run_one(rep.run_id, mid, deadline, retries)
        finally:
            if dm is not None:
                rep.deadline_hit = dm._paused() == "run time budget reached"
                dm.deadline = None
        return self._finish(rep, outcomes)

    def _run_one(self, run_id: str, mid: str, deadline, retries: int):
        if deadline and self.ctx.now() >= deadline:
            self.jobs.update(run_id, mid, status=SKIPPED, reason="time budget reached before this module could start")
            return None
        attempt = 0
        while True:
            attempt += 1
            self.jobs.update(run_id, mid, status=RUNNING, attempt=attempt, started_at=_iso(self.ctx.now()))
            o = self.registry.run(mid, self.ctx, now=self.ctx.now())
            if o.status == "error" and attempt <= retries:
                self.sleep(self.cfg["retry_delay_seconds"] * attempt)
                continue
            break
        self.jobs.update(run_id, mid, status=OUTCOME_TO_JOB.get(o.status, FAILED), finished_at=_iso(self.ctx.now()), error=o.error,
                         summary=_slim(o.summary), attempt=attempt)
        self.history.append(run_id, o)
        return o

    def _alerts(self, outcomes: dict, rep: RunReport) -> list[dict]:
        al = []
        for mid, o in outcomes.items():
            if o is None:
                al.append({"level": "warning", "module": mid, "message": "not started: the run-time budget was used up (it continues on the next run)"})
                continue
            first = (o.error or "").splitlines()[0] if o.error else ""
            if o.status == "error":
                al.append({"level": "error", "module": mid, "message": f"failed: {first}"})
            elif o.status == "not_configured":
                al.append({"level": "warning", "module": mid, "message": f"not configured: {first}"})
            elif o.status in ("unknown", "invalid"):
                al.append({"level": "error", "module": mid, "message": f"cannot run: {first}"})
            else:
                s = o.summary or {}
                if s.get("status") == "no_data":
                    if rep.deadline_hit:       # running out of time on a first full run is expected, not a failure
                        al.append({"level": "warning", "module": mid, "message": "no results yet: the run-time budget ran out before any stock finished (continues next run)"})
                    else:
                        al.append({"level": "error", "module": mid, "message": "produced no data (previous results left untouched)"})
                elif s.get("status") == "partial" or s.get("failed") or s.get("deferred"):
                    al.append({"level": "warning", "module": mid, "message": f"partial results: {s.get('failed', 0)} failed, {s.get('deferred', 0)} deferred stock(s)"})
            for p in o.output_problems:
                al.append({"level": "warning", "module": mid, "message": f"output check: {p}"})
        if rep.deadline_hit:
            al.append({"level": "warning", "module": "*", "message": "the run-time budget was reached: remaining work continues on the next run"})
        try:
            dm = self.ctx.data_manager()
            if dm.stats()["breaker_tripped"]:
                al.append({"level": "warning", "module": "*", "message": "the data provider rate-limited us: downloads were paused (circuit breaker)"})
            if len(dm.ledger.duplicates_for_day(self.ctx.now().strftime("%Y-%m-%d"))):
                al.append({"level": "warning", "module": "*", "message": "possible duplicate downloads today - see Data Management"})
        except Exception:
            pass
        return al

    def _finish(self, rep: RunReport, outcomes: dict) -> RunReport:
        for mid, o in outcomes.items():
            rep.modules[mid] = ({"status": "skipped", "seconds": 0, "summary": {}, "error": None} if o is None else
                                {"status": o.status, "seconds": round(o.seconds, 1), "summary": _slim(o.summary), "error": o.error,
                                 "output_problems": o.output_problems})
        rep.alerts += self._alerts(outcomes, rep)
        rep.finished_at = _iso(self.ctx.now())
        rep.exit_code = 1 if any(a["level"] == "error" for a in rep.alerts) else 0
        atomic_write_text(self.status_path, json.dumps(rep.as_dict(), indent=1, ensure_ascii=False) + "\n")
        return rep
