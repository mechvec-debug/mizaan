"""Shariah Compliance Screener - orchestration.

ONE evaluation path (`evaluate_stock`) serves the daily batch, the dashboard single-stock check and any future module; the
original had two diverging copies (app.py and auto_watchdog.py) that could give different verdicts for the same stock.

Data comes from the shared Data Manager: `info` and `statements` are the SAME cached blobs the Fundamental screener uses, so
running both screeners costs no extra Yahoo calls. XBRL (optional, needs a BSE code) goes through the same cache and ledger.
Output keeps the original schema ({last_updated, data[...]} with ticker, bse_code, sector, market_cap_cr, financial_date,
is_compliant, failure_reason, metrics{6x(metric, threshold, status)}); new fields are additive.
"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ...core.errors import DataUnavailableError
from ...core.io import atomic_write_text
from .bundles import bundle_from_xbrl, bundle_from_yahoo
from .criteria import evaluate
from .evidence import EvidenceStore
from .mapper import BseMapper
from .params import COMPLIANT, CRITERIA, NON_COMPLIANT, UNDETERMINED, ShariahParams

MODULE_ID, DISPLAY_NAME = "shariah", "Shariah Compliance Screener"


@dataclass
class ScanResult:
    records: list = field(default_factory=list)
    failed: dict = field(default_factory=dict)
    carried: list = field(default_factory=list)
    notes: dict = field(default_factory=dict)          # ticker -> data-quality notes (source fallbacks, ignored evidence ...)
    meta: dict = field(default_factory=dict)


def _fmt_date(iso: str | None) -> str:
    return datetime.strptime(iso, "%Y-%m-%d").strftime("%d %b %Y") if iso else "Unknown"


def evaluate_stock(sym: str, dm, mapper: BseMapper, evidence: EvidenceStore, params: ShariahParams = ShariahParams(),
                   xbrl_provider=None, now: datetime | None = None) -> tuple[dict, list]:
    """Returns (record, notes). Raises DataUnavailableError only when Yahoo `info` itself is unavailable."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    notes: list[str] = []
    info = dm.get_info(sym).data
    raw_mcap = info.get("_fast_market_cap") or info.get("marketCap")            # same preference order as the original
    mcap = round(raw_mcap / 1e7, 2) if raw_mcap else None
    sector = info.get("sector") or "Diversified"
    bse = mapper.resolve(sym)

    stmts = None
    try:
        stmts = dm.get_statements(sym).data
    except DataUnavailableError as exc:
        notes.append(f"statements unavailable ({exc.reason})")

    cands = []
    y = bundle_from_yahoo(stmts) if stmts else None
    if y:
        cands.append(y)
    if xbrl_provider is not None and bse:
        try:
            x = dm.get_xbrl_facts(sym, bse, xbrl_provider).data
            xb = bundle_from_xbrl(x["facts"]) if x.get("found") else None
            if xb:
                cands.append(xb)
            else:
                notes.append("no usable annual XBRL filing found" if not x.get("found") else "XBRL filing not usable (not annual / no total assets)")
        except DataUnavailableError as exc:
            notes.append(f"XBRL unavailable ({exc.reason})")
        except Exception as exc:                       # XBRL is an OPTIONAL source: whatever goes wrong there never fails the stock
            notes.append(f"XBRL error ({type(exc).__name__}: {exc}); Yahoo statements used instead")
    # one bundle only (never mix sources or years): the latest fiscal year wins, XBRL wins a tie
    bundle = max(cands, key=lambda b: (b["fy_end"], b["source"] == "xbrl")) if cands else None

    ev_used, ev_notes = ({}, [])
    if bundle:
        ev_used, ev_notes = evidence.for_fy(sym, bundle["fy_end"])
        notes += ev_notes
        verdict, out = evaluate(bundle["p"], bundle["to_crore"], mcap, ev_used, params)
    else:                                                                       # nothing to evaluate: every criterion undetermined
        verdict, out = evaluate({}, None, mcap, {}, params)
        notes.append("no financial statements available")

    breached = [n for n, o in out.items() if o.status is False]
    pending = [{"criterion": n, "reason": o.note} for n, o in out.items() if o.status is None]
    rec = {
        "ticker": sym, "bse_code": bse or "N/A", "sector": sector, "market_cap_cr": mcap if mcap is not None else 0.0,
        "financial_date": _fmt_date(bundle["fy_end"]) if bundle else "Unknown",
        "is_compliant": verdict == COMPLIANT,
        "failure_reason": "" if verdict == COMPLIANT else ("Criteria breach" if verdict == NON_COMPLIANT else "Undetermined: data pending"),
        "metrics": {n: {"metric": o.metric, "threshold": o.threshold, "status": o.status} for n, o in out.items()},
        # ---- additive fields
        "verdict": verdict, "breached": breached, "undetermined": pending,
        "data_source": bundle["source"] if bundle else "none", "fiscal_year_end": bundle["fy_end"] if bundle else None,
        "xbrl_scale": bundle.get("scale") if bundle else None, "evidence_used": sorted(ev_used),
        "evaluated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    return rec, notes


def scan_universe(tickers, dm, mapper: BseMapper, evidence: EvidenceStore, params: ShariahParams = ShariahParams(),
                  xbrl_provider=None, now: datetime | None = None, previous: list | None = None) -> ScanResult:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    tickers = sorted(dict.fromkeys(tickers))                                    # the original listed tickers sorted and de-duplicated
    res = ScanResult()
    for sym in tickers:
        try:
            rec, notes = evaluate_stock(sym, dm, mapper, evidence, params, xbrl_provider, now)
            res.records.append(rec)
            if notes:
                res.notes[sym] = notes
        except DataUnavailableError as exc:
            res.failed[sym] = f"data: {exc.reason}"
        except Exception as exc:                                                # one bad stock never stops the run
            res.failed[sym] = f"engine: {type(exc).__name__}: {exc}"
    prev = {r.get("ticker"): r for r in (previous or [])}
    for sym in list(res.failed):                                                # annual screen: keep the last verdict, dated, for a while
        old = prev.get(sym)
        try:
            age = now - datetime.strptime(old["evaluated_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except (TypeError, KeyError, ValueError):
            continue
        if age <= timedelta(days=params.carry_forward_days):
            res.records.append(old)
            res.carried.append(sym)
    res.records.sort(key=lambda r: r["ticker"])
    verdicts = Counter(r["verdict"] if "verdict" in r else (COMPLIANT if r["is_compliant"] else NON_COMPLIANT) for r in res.records)
    pend = Counter(u["criterion"] for r in res.records for u in r.get("undetermined", []))
    res.meta = {"module": MODULE_ID, "display_name": DISPLAY_NAME, "run_at": now.isoformat(timespec="seconds"),
                "requested": len(tickers), "evaluated": len(res.records) - len(res.carried), "carried_forward": len(res.carried),
                "failed": len(res.failed), "by_verdict": dict(verdicts), "undetermined_by_criterion": dict(pend),
                "by_source": dict(Counter(r.get("data_source", "legacy") for r in res.records)),
                "status": "no_data" if not res.records else ("partial" if res.failed else "ok")}
    return res


def write_outputs(res: ScanResult, out_dir: str | Path, extra_meta: dict | None = None, now: datetime | None = None) -> dict:
    out = Path(out_dir)
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    report = {**res.meta, **(extra_meta or {}), "failed_detail": res.failed, "carried_forward_detail": res.carried, "data_notes": res.notes}
    paths = {"report": str(atomic_write_text(out / "run_report.json", json.dumps(report, ensure_ascii=False, indent=1) + "\n"))}
    if res.records:                                                             # nothing produced -> previous results stay untouched
        payload = {"last_updated": now.strftime("%d %b %Y, %I:%M %p"), "data": res.records}
        paths["data"] = str(atomic_write_text(out / "latest_results.json", json.dumps(payload, ensure_ascii=False, indent=2) + "\n"))
    return paths


def run(universe, dm, out_dir, mapper: BseMapper, evidence: EvidenceStore, params: ShariahParams = ShariahParams(),
        xbrl_provider=None, now: datetime | None = None) -> dict:
    out = Path(out_dir)
    prev = None
    if (out / "latest_results.json").exists():
        try:
            prev = json.loads((out / "latest_results.json").read_text(encoding="utf-8")).get("data")
        except (OSError, ValueError):
            prev = None
    res = scan_universe(universe.tickers, dm, mapper, evidence, params, xbrl_provider, now, prev)
    st = dm.stats()
    extra = {"universe_version": universe.version, "universe_count": universe.count, "data_run_id": dm.run_id,
             "downloads": st["downloads"], "cache_hits": st["cache_hits"], "breaker_tripped": st["breaker_tripped"]}
    write_outputs(res, out, extra, now)
    dm.close()
    return {**res.meta, **extra}
