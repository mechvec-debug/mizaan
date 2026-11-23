"""Cross-screener view (optional feature, kept separate from the original outputs).

Each screener keeps its own verdict, score and ranking; this module only JOINS them by ticker so you can ask
"which of today's swing setups are Shariah-compliant?". Nothing is averaged, combined or re-scored.
Important: UNDETERMINED is not COMPLIANT. The summary matrix keeps the three Shariah verdicts apart.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from ..core.io import atomic_write_text

TECH_ACTIONABLE = ("BUY TRIGGERED", "IN BUY ZONE", "WAIT FOR BUY ZONE")
_GRADE = {"A": 0, "B": 1, "C": 2}


def _load(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def load_results(store_root: str | Path) -> dict:
    """Latest published results of each screener from the data store (missing file -> empty list)."""
    r = Path(store_root) / "results"
    tech = _load(r / "technical" / "trading_alerts.json") or []
    fund = _load(r / "fundamental" / "data.json") or []
    shar = (_load(r / "shariah" / "latest_results.json") or {}).get("data", [])
    return {"technical": tech, "fundamental": fund, "shariah": shar}


def _is_actionable(action: str | None) -> bool:
    return bool(action) and any(a in action for a in TECH_ACTIONABLE)


def cross_view(technical: list, fundamental: list, shariah: list) -> dict:
    t = {r["ticker"]: r for r in technical}
    f = {r["Ticker"]: r for r in fundamental}
    s = {r["ticker"]: r for r in shariah}
    rows = []
    for tk in sorted(set(t) | set(f) | set(s)):
        tr, fr, sr = t.get(tk), f.get(tk), s.get(tk)
        d = (tr or {}).get("details", {})
        rows.append({
            "ticker": tk,
            "technical": None if not tr else {"action": tr["action"], "grade": d.get("grade") or "", "entry": tr.get("entry"), "sl": tr.get("sl"),
                                              "tp1": tr.get("tp1"), "notify_price": d.get("notify_price"), "plan": tr.get("plan"),
                                              "changed_today": bool(d.get("changed_today")), "actionable": _is_actionable(tr["action"])},
            "fundamental": None if not fr else {"status": fr["Status"], "tech_score": fr.get("Tech Score"), "drawdown_pct": fr.get("Drawdown %")},
            "shariah": None if not sr else {"verdict": sr.get("verdict") or ("COMPLIANT" if sr.get("is_compliant") else "NON_COMPLIANT"),
                                            "breached": sr.get("breached", []), "pending": [u["criterion"] for u in sr.get("undetermined", [])]},
        })
    # technical setups first (changed today, best grade), then everything else by ticker - Shariah never changes the technical order
    rows.sort(key=lambda r: (not (r["technical"] and r["technical"]["actionable"]),
                             not (r["technical"] and r["technical"]["changed_today"]),
                             _GRADE.get((r["technical"] or {}).get("grade", ""), 3), r["ticker"]))
    act = [r for r in rows if r["technical"] and r["technical"]["actionable"]]
    matrix = Counter((r["shariah"]["verdict"] if r["shariah"] else "NOT_SCREENED") for r in act)
    return {
        "rows": rows,
        "summary": {
            "stocks": len(rows), "in_technical": len(t), "in_fundamental": len(f), "in_shariah": len(s),
            "technical_actionable": len(act),
            "technical_actionable_by_shariah": dict(matrix),
            "technical_actionable_and_shariah_compliant": [r["ticker"] for r in act if r["shariah"] and r["shariah"]["verdict"] == "COMPLIANT"],
            "technical_actionable_shariah_undetermined": [r["ticker"] for r in act if r["shariah"] and r["shariah"]["verdict"] == "UNDETERMINED"],
            "note": "UNDETERMINED is not COMPLIANT: those stocks still lack evidence for at least one criterion.",
        },
    }


def write_cross_view(store_root: str | Path, out_path: str | Path | None = None) -> dict:
    res = load_results(store_root)
    view = cross_view(res["technical"], res["fundamental"], res["shariah"])
    atomic_write_text(out_path or Path(store_root) / "results" / "cross_view.json", json.dumps(view, ensure_ascii=False, indent=1) + "\n")
    return view
