"""Annual Report Intelligence - dashboard numbers (written to results/annual_report/dashboard.json for every front end)."""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from pathlib import Path

from ..core.io import atomic_write_text
from .state import DONE, FAILED, FAILED_FINAL, NO_REPORT, PENDING, PROCESSING, UNCHECKED


def summary(q, u, cfg, now: datetime) -> dict:
    act = {s: e for s, e in q.entries.items() if e["status"] != "removed"}
    cnt = lambda *st: sum(1 for e in act.values() if e["status"] in st)
    day = now.strftime("%Y-%m-%d")
    t = u.data.get(day, {"stocks": 0, "tokens_in": 0, "tokens_out": 0, "requests": 0, "stopped": None, "alerts": [], "discovered": 0})
    used, budget = t["tokens_in"] + t["tokens_out"], cfg.daily_token_budget
    pending, retry = cnt(PENDING, PROCESSING), cnt(FAILED)
    work_left = pending + retry + cnt(UNCHECKED)
    last = max((e["last_success"] for e in act.values() if e["last_success"]), default=None)
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    next_batch = None
    if work_left:
        next_batch = f"{tomorrow} {cfg.schedule_hour_utc:02d}:00 UTC" if t["stopped"] else f"{day} {cfg.schedule_hour_utc:02d}:00 UTC"
    return {
        "as_of": now.strftime("%Y-%m-%d %H:%M UTC"),
        "total_eligible_stocks": len(act),
        "reports_available": sum(1 for e in act.values() if e["report"]),
        "reports_processed": cnt(DONE),
        "reports_pending": pending,                        # waiting for analysis; available = processed + pending + failed
        "reports_failed": cnt(FAILED, FAILED_FINAL),        # of which `failed_awaiting_retry` will be retried automatically
        "failed_awaiting_retry": retry,
        "no_report_found": cnt(NO_REPORT),
        "not_yet_checked": cnt(UNCHECKED),
        "inconclusive": sum(1 for e in act.values() if e["status"] == DONE and e["analyzed"] and
                            list(e["analyzed"].values())[-1].get("outcome") == "inconclusive"),
        "daily_batch": {"date": day, "stocks_processed": t["stocks"], "stocks_limit": cfg.stock_cap, "reports_looked_up": t.get("discovered", 0),
                        "stopped_reason": t["stopped"], "alerts": t["alerts"]},
        "tokens": {"today_in": t["tokens_in"], "today_out": t["tokens_out"], "today_total": used, "daily_budget": budget,
                   "today_fraction": round(used / budget, 3) if budget else None,
                   "lifetime_total": sum(d["tokens_in"] + d["tokens_out"] for d in u.data.values()),
                   "last_14_days": {d: u.data[d]["tokens_in"] + u.data[d]["tokens_out"] for d in sorted(u.data)[-14:]}},
        "last_successful_processing_date": last,
        "next_scheduled_batch": next_batch,
        "estimated_days_to_finish_upper_bound": math.ceil(work_left / cfg.stock_cap) if cfg.stock_cap and work_left else 0,
        "failed_detail": [{"symbol": s, "status": e["status"], "attempts": e["attempts"], "error": e["last_error"], "retry_after": e["retry_after"]}
                          for s, e in sorted(act.items()) if e["status"] in (FAILED, FAILED_FINAL)][:100],
    }


def write_dashboard(store_dir, q, u, cfg, now: datetime) -> dict:
    s = summary(q, u, cfg, now)
    atomic_write_text(Path(store_dir) / "results" / "annual_report" / "dashboard.json", json.dumps(s, indent=1, ensure_ascii=False) + "\n")
    return s
