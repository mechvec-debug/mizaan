"""View-models: everything a page shows, computed from the published results in the data store. Pure Python (no Streamlit),
so each page's numbers are unit-tested without a browser. Missing files never raise: pages get empty views with a reason."""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..overview.cross import cross_view, load_results
from ..screeners.shariah.params import CRITERIA


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _res(store, module, name):
    return Path(store) / "results" / module / name


# ---------------------------------------------------------------- Screener 1 - Swing Setup Radar
TECH_ROW_KEYS = ("ticker", "action", "entry", "sl", "tp1", "tp2", "tp3", "aligned", "plan", "time_received")


def technical_rows(records: list) -> list[dict]:
    out = []
    for r in records:
        d = r.get("details", {})
        out.append({"Stock": r["ticker"], "Signal": r["action"], "Grade": d.get("grade") or "", "Why": d.get("reason", ""),
                    "Entry (₹)": r.get("entry"), "Stop Loss (₹)": r.get("sl"), "TP1 (₹)": r.get("tp1"), "TP2 (₹)": r.get("tp2"),
                    "TP3 (₹)": r.get("tp3"), "Notify at (₹)": d.get("notify_price"), "Zone low": d.get("zone_lo"), "Zone high": d.get("zone_hi"),
                    "RR": d.get("rr_at_close"), "MTF Alignment": r.get("aligned"), "Trade Plan": r.get("plan"),
                    "New today": "yes" if d.get("changed_today") else "", "Since": d.get("state_since"), "Data": d.get("data_status", "")})
    return out


def technical_view(store) -> dict:
    from ..screeners.technical.filters import original_visible, views
    records = read_json(_res(store, "technical", "trading_alerts.json"), []) or []
    v = views(records) if records else {"actionable": [], "watch": [], "all": []}
    orig = original_visible(records) if records else None
    return {"empty": not records, "report": read_json(_res(store, "technical", "run_report.json"), {}),
            "actionable": technical_rows(v["actionable"]), "watch": technical_rows(v["watch"]), "all": technical_rows(v["all"]),
            "original": orig, "display_date": (orig["time_received"].dt.date.max().strftime("%d %b %Y") if orig is not None and not orig.empty else None)}


# ---------------------------------------------------------------- Screener 2 - Quality Value Rebound
BASIC = ["Ticker", "Market Cap", "Sector", "Current price"]
TRADE = ["Ticker", "Sector", "Current price", "Drawdown %", "Tech Score", "Stop Loss", "Target 1", "Target 2", "Risk Reward"]
WATCH = ["Ticker", "Sector", "Current price", "Drawdown %", "Tech Score", "Notes"]
WHY = ["Ticker", "Sector", "Current price", "Notes"]
# (status in data, heading, caption, columns) - the original app's sections. Caption numbers corrected to the CODE values (35-65 %).
FUND_LEFT = [("🔥 PASS", "🔥 Execution Ready (Pass)", "Strong fundamentals, fair valuation, 35-65% off the 3-year high, bottom formed, daily trend turning up.", TRADE),
             ("WAIT (Price)", "⏳ Waiting for Price", "", BASIC + ["Drawdown %"]),
             ("WAIT (Valuation)", "💰 Waiting for Valuation", "", BASIC + ["PE"])]
FUND_RIGHT = [("👀 WATCH", "👀 Watchlist (Near Buy Zone)", "Good business and valuation, price has fallen 25% or more, but chart confirmation is still missing.", WATCH),
              ("WAIT (Fund)", "⚠️ Fundamental Wait (Fails Criteria)", "", WHY)]


def fundamental_view(store) -> dict:
    rows = read_json(_res(store, "fundamental", "data.json"), []) or []

    def section(status, cols):
        return [{c: r.get(c) for c in cols if c in r} for r in rows if r.get("Status") == status]
    return {"empty": not rows, "rows": rows, "report": read_json(_res(store, "fundamental", "run_report.json"), {}),
            "last_scan": rows[0].get("Updated") if rows else None,
            "left": [(h, cap, section(s, cols)) for s, h, cap, cols in FUND_LEFT],
            "right": [(h, cap, section(s, cols)) for s, h, cap, cols in FUND_RIGHT]}


# ---------------------------------------------------------------- Screener 3 - Shariah
def _state(status):
    return "pass" if status is True else ("fail" if status is False else "pending")


def shariah_view(store) -> dict:
    payload = read_json(_res(store, "shariah", "latest_results.json"), {}) or {}
    data = payload.get("data", [])
    verdict = lambda d: d.get("verdict") or ("COMPLIANT" if d.get("is_compliant") else "NON_COMPLIANT")
    vc = Counter(verdict(d) for d in data)
    total = len(data)
    items = []
    for d in data:
        pend = {u["criterion"]: u["reason"] for u in d.get("undetermined", [])}
        cards = []
        for name in CRITERIA:
            m = d.get("metrics", {}).get(name, {})
            st = _state(m.get("status", m.get("passed")))
            cards.append({"label": name, "value": m.get("value") or m.get("metric", "-"), "limit": m.get("threshold", "-"), "state": st,
                          "note": pend.get(name, ""), "badge": {"pass": "✅ PASS", "fail": "❌ FAIL", "pending": "⏳ PENDING"}[st]})
        v = verdict(d)
        items.append({"ticker": d["ticker"], "sector": d.get("sector", ""), "market_cap_cr": d.get("market_cap_cr", 0), "period": d.get("financial_date", "Annual FY"),
                      "verdict": v, "tag": {"COMPLIANT": "🟢 COMPLIANT", "NON_COMPLIANT": "🔴 NON-COMPLIANT", "UNDETERMINED": "🟡 UNDETERMINED"}[v],
                      "cards": cards, "source": d.get("data_source", ""), "bse_code": d.get("bse_code", "")})
    return {"empty": not data, "last_updated": payload.get("last_updated", ""), "items": items, "report": read_json(_res(store, "shariah", "run_report.json"), {}),
            "kpis": {"total": total, "compliant": vc.get("COMPLIANT", 0), "non_compliant": vc.get("NON_COMPLIANT", 0), "undetermined": vc.get("UNDETERMINED", 0),
                     "rate": round(vc.get("COMPLIANT", 0) / total * 100, 1) if total else 0}}


# ---------------------------------------------------------------- Annual report
def annual_view(store) -> dict:
    d = read_json(_res(store, "annual_report", "dashboard.json"))
    return {"empty": d is None, "d": d or {}}


# ---------------------------------------------------------------- Executive overview
def overview_view(store) -> dict:
    res = load_results(store)
    t, f, s = technical_view(store), fundamental_view(store), shariah_view(store)
    fc = Counter(r.get("Status") for r in res["fundamental"])
    return {"empty": not any(res.values()),
            "technical": {"stocks": len(res["technical"]), "actionable": len(t["actionable"]), "watch": len(t["watch"])},
            "fundamental": {"stocks": len(res["fundamental"]), "pass": fc.get("🔥 PASS", 0), "watch": fc.get("👀 WATCH", 0)},
            "shariah": s["kpis"], "cross": cross_view(res["technical"], res["fundamental"], res["shariah"])}


# ---------------------------------------------------------------- Data management
def data_management_view(store, universe_service=None, today: str | None = None) -> dict:
    from ..data.ledger import DownloadLedger
    from ..data.store import RawStore
    store = Path(store)
    today = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out = {"size": RawStore(store / "raw").size_report() if (store / "raw").exists() else {"files": 0, "mb": 0, "by_type_mb": {}}}
    if (store / "ledger").exists():
        led = DownloadLedger(store / "ledger", "view")
        df = led.read()
        out["ledger_rows"] = len(df)
        out["recent_downloads"] = df.tail(30).iloc[::-1].to_dict("records") if len(df) else []
        out["downloads_today"] = led.counts_for_day(today).to_dict("records")
        out["duplicates_today"] = led.duplicates_for_day(today).to_dict("records")
        out["failures_recent"] = df[df["status"] != "ok"].tail(15).iloc[::-1].to_dict("records") if len(df) else []
    else:
        out.update(ledger_rows=0, recent_downloads=[], downloads_today=[], duplicates_today=[], failures_recent=[])
    limit = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=7)).strftime("%Y-%m-%d")
    metas = [read_json(p, {}) for p in (store / "raw" / "ohlcv").glob("*/*.meta.json")] if (store / "raw" / "ohlcv").exists() else []
    out["price_history"] = {"stocks": len(metas), "stale_over_7_days": sum(1 for m in metas if m.get("last_date", "") < limit),
                            "oldest_last_bar": min((m.get("last_date", "") for m in metas), default=None)}
    if universe_service is not None:
        try:
            snap = universe_service.get()
            out["universe"] = {**snap.summary(), "invalid": [{"line": e.line_no, "text": e.raw, "reason": e.reason} for e in snap.invalid],
                               "duplicates": len(snap.duplicates), "disabled": [e.symbol for e in snap.disabled]}
        except Exception as exc:
            out["universe"] = {"error": f"{type(exc).__name__}: {exc}"}
    ev = store / "state" / "shariah_evidence"
    out["evidence_stocks"] = len(list(ev.glob("*.json"))) if ev.exists() else 0
    return out


# ---------------------------------------------------------------- Execution monitor
def execution_view(store, registry=None, cfg=None) -> dict:
    from ..execution.history import HistoryStore
    from ..execution.schedule import next_cron_run
    from ..publish.contract import validate_published
    h = HistoryStore(Path(store) / "state" / "execution" / "history.json")
    reports = {}
    for mid in ("technical", "fundamental", "shariah"):
        r = read_json(_res(store, mid, "run_report.json"))
        if r:
            reports[mid] = {k: r.get(k) for k in ("run_at", "status", "requested", "evaluated", "scanned", "skipped", "failed", "carried_forward", "deferred", "by_status", "by_verdict", "by_state") if k in r}
            reports[mid]["failed_detail"] = r.get("failed_detail") or {}
    ann = read_json(_res(store, "annual_report", "dashboard.json"), {})
    status = read_json(Path(store) / "state" / "execution" / "status.json", {}) or {}
    cron = ((cfg or {}).get("execution") or {}).get("schedule_cron_utc")
    nxt = next_cron_run(cron, datetime.now(timezone.utc)) if cron else None
    man = read_json(Path(store) / "published" / "v1" / "manifest.json", {}) or {}
    pub = {"generated_at": man.get("generated_at"), "files": len(man.get("files", {})), "missing": man.get("missing", []),
           "problems": validate_published(Path(store) / "published" / "v1") if man else []}
    return {"status": status, "next_run": nxt.strftime("%Y-%m-%d %H:%M UTC") if nxt else None, "published": pub,
            "history": h.recent(50), "last": h.last_by_module(), "reports": reports,
            "annual_alerts": (ann.get("daily_batch") or {}).get("alerts", []),
            "modules": [{"id": s.id, "name": s.name, "category": s.category, "enabled": registry.is_enabled(s.id)} for s in registry.all()] if registry else []}


# ---------------------------------------------------------------- Custom modules + configuration
def generic_module_view(registry, ctx, module_id: str) -> dict:
    spec = registry.get(module_id)
    rows = registry.load_rows(spec, ctx) if spec else None
    return {"spec": spec, "rows": rows if isinstance(rows, list) else ([] if rows is None else [rows]), "missing": rows is None}


def config_view(cfg: dict, registry, universe_service) -> dict:
    mods = [{"id": s.id, "name": s.name, "category": s.category, "enabled": registry.is_enabled(s.id), "built_in": s.builtin,
             "needs": ", ".join(s.data_dependencies), "universe_filter": s.universe_filter} for s in registry.all()]
    return {"cfg": cfg, "modules": mods, "invalid_modules": dict(registry.invalid), "universe_file": str(universe_service.path)}
