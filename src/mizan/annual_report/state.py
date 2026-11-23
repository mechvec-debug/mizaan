"""Persistent state (JSON, git-friendly, atomic writes): the queue, daily usage and the saved analyses.

queue.json    one entry per universe stock: status, current report (version), attempts, retry date, analysed versions
usage.json    per calendar day: stocks processed, tokens in/out, requests, why the batch stopped, alerts
analyses/<SYMBOL>.json   every completed analysis with the fields the requirements list
Every state change is saved immediately, so a crash loses at most the stock being processed.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from ..core.io import atomic_write_text
from ..data.store import safe_name

UNCHECKED, NO_REPORT, PENDING, PROCESSING, DONE, FAILED, FAILED_FINAL, REMOVED = (
    "unchecked", "no_report", "pending", "processing", "done", "failed", "failed_final", "removed")


def _read(path: Path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _write(path: Path, obj) -> None:
    atomic_write_text(path, json.dumps(obj, indent=1, sort_keys=True, ensure_ascii=False) + "\n")


def new_entry() -> dict:
    return {"status": UNCHECKED, "report": None, "attempts": 0, "last_error": None, "retry_after": None, "last_checked": None,
            "analyzed": {}, "last_success": None, "processing_since": None, "prev_status": None}


class QueueStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.entries: dict = _read(self.path, {}).get("entries", {})

    def save(self) -> None:
        _write(self.path, {"version": 1, "entries": self.entries})

    def sync(self, tickers) -> None:
        """Make the queue mirror the universe: new stocks start unchecked; stocks that left are marked removed (history kept)."""
        want = set(tickers)
        for s in want:
            e = self.entries.setdefault(s, new_entry())
            if e["status"] == REMOVED:
                e["status"] = UNCHECKED
        for s, e in self.entries.items():
            if s not in want and e["status"] != REMOVED:
                e["status"] = REMOVED
        self.save()

    def active(self) -> list[str]:
        return sorted(s for s, e in self.entries.items() if e["status"] != REMOVED)

    def recover_stale(self, now: datetime, timeout_hours: float, force: bool = False) -> list[str]:
        """A crashed run leaves 'processing' behind. Entries older than the timeout (or all of them with force=True, for a manual
        restart right after a crash) go back to their previous status; the interrupted attempt is counted."""
        out = []
        for s, e in self.entries.items():
            if e["status"] != PROCESSING:
                continue
            since = datetime.fromisoformat(e["processing_since"]) if e["processing_since"] else None
            if force or since is None or now - since >= timedelta(hours=timeout_hours):
                e["status"] = e.get("prev_status") or PENDING
                e["attempts"] += 1
                e["last_error"] = "interrupted run (recovered)"
                out.append(s)
        if out:
            self.save()
        return out

    def counts(self) -> dict:
        c: dict = {}
        for e in self.entries.values():
            c[e["status"]] = c.get(e["status"], 0) + 1
        return c


class UsageStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.data: dict = _read(self.path, {}).get("days", {})

    def day(self, date: str) -> dict:
        return self.data.setdefault(date, {"stocks": 0, "discovered": 0, "tokens_in": 0, "tokens_out": 0, "requests": 0,
                                           "stopped": None, "alerts": []})

    def save(self) -> None:
        _write(self.path, {"days": self.data})


class AnalysisStore:
    def __init__(self, directory: str | Path):
        self.dir = Path(directory)

    def _path(self, sym: str) -> Path:
        return self.dir / f"{safe_name(sym)}.json"

    def records(self, sym: str) -> list:
        return _read(self._path(sym), {}).get("records", [])

    def add(self, sym: str, record: dict) -> None:
        recs = self.records(sym) + [record]
        _write(self._path(sym), {"symbol": sym, "records": recs})
