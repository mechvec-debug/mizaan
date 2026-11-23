"""Minimal sequential runner (the full scheduler, retries and resume come in Phase 10).
Runs the chosen modules one after another; each is isolated by the registry, so one failure never stops the rest."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from .history import HistoryStore


def run_modules(registry, ctx, ids: list[str] | None = None, history: HistoryStore | None = None, run_id: str | None = None) -> list:
    ctx.registry = registry
    run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    history = history or HistoryStore(ctx.store / "state" / "execution" / "history.json")
    todo = ids if ids is not None else [s.id for s in registry.enabled()]
    outcomes = []
    for mid in todo:
        o = registry.run(mid, ctx, now=ctx.now())
        history.append(run_id, o)
        outcomes.append(o)
    return outcomes
