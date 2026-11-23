"""Evidence store: the figures the statements cannot give (haram revenue, impermissible income, non-compliant investments).

Written by the annual-report analysis (Phase 8) or by an admin; read by the Shariah screener. No number enters without
its source, fiscal year and a written justification ("no assumption"). Evidence only counts for the SAME fiscal year as
the statements it is compared with; older evidence is reported as ignored, never silently reused.
One small JSON file per ticker (git-friendly), amounts in Rs crore.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from ...core.io import atomic_write_text
from ...data.store import safe_name

FIELDS = ("haram_business_revenue", "impermissible_interest_income", "non_compliant_investments")
SOURCES = ("manual", "ai", "xbrl")


class EvidenceStore:
    def __init__(self, root: str | Path):
        self.dir = Path(root)

    def _path(self, symbol: str) -> Path:
        return self.dir / f"{safe_name(symbol)}.json"

    def get(self, symbol: str) -> dict:
        p = self._path(symbol)
        if not p.exists():
            return {}
        with open(p, encoding="utf-8") as fh:
            return json.load(fh).get("records", {})

    def set(self, symbol: str, field: str, value_cr: float, *, source: str, fy_end: str, evidence: str,
            model: str | None = None, now: datetime | None = None) -> None:
        if field not in FIELDS:
            raise ValueError(f"unknown evidence field {field!r}; expected one of {FIELDS}")
        if source not in SOURCES:
            raise ValueError(f"source must be one of {SOURCES}")
        if value_cr is None or value_cr < 0:
            raise ValueError("value must be a non-negative amount in Rs crore (use 0 only when the filing states nil)")
        if not (evidence or "").strip():
            raise ValueError("evidence text is required: say where in the filing the figure comes from")
        recs = self.get(symbol)
        recs[field] = {"value_cr": float(value_cr), "source": source, "fy_end": fy_end, "evidence": evidence.strip(), "model": model,
                       "recorded_at": (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")}
        atomic_write_text(self._path(symbol), json.dumps({"symbol": symbol, "records": recs}, indent=1, sort_keys=True, ensure_ascii=False) + "\n")

    def for_fy(self, symbol: str, fy_end: str | None) -> tuple[dict, list]:
        """({field: value_cr} for evidence matching fy_end, [notes about evidence that was ignored])."""
        used, notes = {}, []
        for f, r in self.get(symbol).items():
            if fy_end and r.get("fy_end") == fy_end:
                used[f] = r["value_cr"]
            else:
                notes.append(f"{f}: evidence is for FY {r.get('fy_end')}, statements are FY {fy_end} - ignored")
        return used, notes
