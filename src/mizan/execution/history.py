"""Persistent run history (state/execution/history.json) - what ran, when, how long, what happened. Survives restarts."""
from __future__ import annotations

import json
from pathlib import Path

from ..core.io import atomic_write_text

KEEP = 500


def _slim(summary: dict) -> dict:
    """Keep the history file small: scalars, short lists and small mappings only."""
    out = {}
    for k, v in (summary or {}).items():
        if isinstance(v, (str, int, float, bool)) or v is None:
            out[k] = v
        elif isinstance(v, (list, tuple)) and len(v) <= 20:
            out[k] = list(v)
        elif isinstance(v, dict) and len(v) <= 20:
            out[k] = v
    return out


class HistoryStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def all(self) -> list:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []

    def append(self, run_id: str, outcome) -> None:
        rec = {"run_id": run_id, **{k: v for k, v in outcome.as_dict().items() if k != "summary"}, "summary": _slim(outcome.summary)}
        atomic_write_text(self.path, json.dumps((self.all() + [rec])[-KEEP:], indent=1, ensure_ascii=False) + "\n")

    def recent(self, n: int = 50) -> list:
        return list(reversed(self.all()[-n:]))

    def last_by_module(self) -> dict:
        out = {}
        for r in self.all():
            out[r["module_id"]] = r
        return out
