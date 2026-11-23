"""Controlled, resumable annual-report batch.

Every daily run:  1) sync the queue with the universe  2) recover a crashed run  3) look up reports (budgeted, separate from AI)
                  4) analyse pending stocks until the 100-stock cap, the token budget, an AI rate limit or the end of the queue
State is saved after every stock, so an interrupted run loses at most the stock in progress and tomorrow's run simply continues.
Never: more than 100 stocks a day, a call that could push the day over the token budget, the same report version twice,
a number into the screener without its evidence, a silent zero.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..core.errors import DataUnavailableError, ProviderError, RateLimitError, TransientProviderError
from ..data.throttle import RateLimiter
from .config import AnnualReportConfig
from .dashboard import write_dashboard
from .extract import build_excerpt, estimate_tokens, pdf_pages, sections_from_xbrl, select_pages
from .llm import LLMOutputError, Usage
from .prompt import FIELDS, build_prompt, validate_output
from .reports import ReportRef
from .state import (DONE, FAILED, FAILED_FINAL, NO_REPORT, PENDING, PROCESSING, UNCHECKED, AnalysisStore, QueueStore, UsageStore)

DISCOVERY_WEIGHT = 6                      # one BSE look-up = several HTTP requests (listing pages + downloads)


class _Stop(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class _Fail(Exception):
    pass


@dataclass
class BatchReport:
    date: str
    stopped: str | None = None
    recovered: list = field(default_factory=list)
    discovered: int = 0
    discovery_errors: dict = field(default_factory=dict)
    processed: list = field(default_factory=list)
    failed: dict = field(default_factory=dict)
    tokens_in: int = 0
    tokens_out: int = 0
    requests: int = 0
    alerts: list = field(default_factory=list)
    remaining: int = 0


def _day(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


def _sections(ref: ReportRef, content: bytes, cfg: AnnualReportConfig) -> list[str]:
    if ref.kind == "xbrl":
        return sections_from_xbrl(content)
    return [f"[page {n}] {t}" for n, t in select_pages(pdf_pages(content), max_pages=cfg.max_pages)]


def _company(dm, sym: str) -> str:
    try:
        d = dm.get_info(sym).data
        return d.get("longName") or d.get("shortName") or sym
    except Exception:
        return sym


def _needs_check(e: dict, today: str, cfg: AnnualReportConfig) -> bool:
    lim = lambda d, n: (datetime.strptime(d, "%Y-%m-%d") + timedelta(days=n)).strftime("%Y-%m-%d") <= today
    if e["status"] == UNCHECKED:
        return e["last_checked"] != today                      # a failed look-up is retried tomorrow, not in a loop today
    if e["status"] == NO_REPORT:
        return lim(e["last_checked"], cfg.recheck_missing_days)
    if e["status"] == DONE:                                    # look for a NEW annual report once a year
        return lim(e["last_success"] or e["last_checked"] or "1970-01-01", cfg.recheck_done_days)
    return False


def _call_llm(llm, prompt: str, cfg: AnnualReportConfig, sleep):
    for i in range(cfg.llm_retries + 1):
        try:
            return llm.generate_json(prompt, cfg.max_output_tokens)
        except RateLimitError:
            raise _Stop("rate_limited")                        # pause until the next scheduled run, nothing is lost
        except TransientProviderError as exc:
            if i == cfg.llm_retries:
                raise _Fail(f"AI service error: {exc}")
            sleep(2.0 * (2 ** i))


def run_batch(tickers, *, store_dir, provider, llm, evidence, dm, mapper, cfg: AnnualReportConfig, now: datetime | None = None,
              sleep=time.sleep, force_recover: bool = False) -> BatchReport:
    cfg.require_limits()                                       # refuses to run without a token budget and a request rate
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    day = _day(now)
    base = Path(store_dir) / "state" / "annual_report"
    q, u, an = QueueStore(base / "queue.json"), UsageStore(base / "usage.json"), AnalysisStore(base / "analyses")
    rep, today = BatchReport(date=day), u.day(day)
    model = getattr(llm, "model", None) or cfg.model          # record the model that was ACTUALLY used

    q.sync(tickers)
    rep.recovered = q.recover_stale(now, cfg.processing_timeout_hours, force_recover)

    # ---------------------------------------------------------------- 1) discovery (BSE look-ups; does not use the AI budget)
    dlim = RateLimiter(cfg.discovery_requests_per_minute, sleep=sleep)
    due = sorted((s for s in q.active() if _needs_check(q.entries[s], day, cfg)),
                 key=lambda s: (q.entries[s]["status"] != UNCHECKED, q.entries[s]["last_checked"] or "", s))
    for s in due[: max(0, cfg.discovery_per_day - today["discovered"])]:
        e = q.entries[s]
        try:
            dlim.acquire(DISCOVERY_WEIGHT)
            ref = provider.find(s, mapper.resolve(s))
        except RateLimitError:
            today["alerts"].append("report look-up paused: BSE rate limit"); break
        except (DataUnavailableError, ProviderError) as exc:
            e["last_checked"], e["last_error"] = day, f"look-up: {exc}"
            rep.discovery_errors[s] = str(exc); q.save(); continue
        e["last_checked"], today["discovered"], rep.discovered = day, today["discovered"] + 1, rep.discovered + 1
        if ref is None:
            e["status"] = e["status"] if e["status"] == DONE else NO_REPORT
        elif ref.version in e["analyzed"]:
            e["status"], e["report"] = DONE, ref.to_dict()     # same report version: never analysed twice
        else:
            e.update(status=PENDING, report=ref.to_dict(), attempts=0, retry_after=None, last_error=None)
        q.save()
    u.save()

    # ---------------------------------------------------------------- 2) analysis
    budget, cap = cfg.daily_token_budget, cfg.stock_cap
    llim = RateLimiter(cfg.requests_per_minute, sleep=sleep)
    eligible = lambda e: e["status"] == PENDING or (e["status"] == FAILED and (e["retry_after"] or "") <= day)
    queue = sorted((s for s in q.active() if eligible(q.entries[s])),
                   key=lambda s: (q.entries[s]["status"] != PENDING, q.entries[s]["retry_after"] or "", s))
    for s in queue:
        used = today["tokens_in"] + today["tokens_out"]
        if today["stocks"] >= cap:
            rep.stopped = "stock_limit"; break
        if used >= budget:
            rep.stopped = "token_budget"; break
        e = q.entries[s]
        prev = e["status"]
        e.update(status=PROCESSING, processing_since=now.isoformat(), prev_status=prev); q.save()      # checkpoint: claimed
        ref = ReportRef.from_dict(e["report"])
        spent, est_in, called = Usage(), 0, False
        try:
            content = provider.fetch(ref)
            sections = _sections(ref, content, cfg)
            if not sections and ref.kind == "xbrl" and cfg.allow_pdf_fallback:
                pref = provider.find_pdf(s, mapper.resolve(s))                                         # PDF = workaround only
                if pref:
                    ref, content = pref, provider.fetch(pref)
                    sections = _sections(ref, content, cfg)
            if not sections:
                raise _Fail("no usable note text in the report (XML has no notes; PDF fallback unavailable or empty)")
            prompt = build_prompt(s, _company(dm, s), ref.fy_end, build_excerpt(sections, cfg.max_input_chars))
            est_in = estimate_tokens(prompt, cfg.chars_per_token)
            if used + est_in + cfg.max_output_tokens > budget:
                raise _Stop("token_budget")                    # this call could push the day over budget: do not start it
            llim.acquire()
            called = True
            data, spent = _call_llm(llm, prompt, cfg, sleep)
            accepted, rejected = validate_output(data)
            fy = ref.fy_end or (data.get("fiscal_year_end") if isinstance(data.get("fiscal_year_end"), str) and len(data["fiscal_year_end"]) == 10 else None)
            written = []
            if fy:
                for f, (v, ev) in accepted.items():
                    evidence.set(s, f, v, source="ai", fy_end=fy, evidence=ev, model=model, now=now); written.append(f)
            else:
                rejected.update({f: "fiscal year of the report unknown" for f in accepted})
            outcome = "done" if written else "inconclusive"
            an.add(s, {"symbol": s, "company_name": _company(dm, s), "fiscal_year_end": fy, "report_source": ref.source,
                       "report_version": ref.version, "report_ref": ref.attachment, "report_date": ref.news_date,
                       "download_date": day, "analysis_date": day, "status": outcome, "ai_model": model,
                       "tokens_in": spent.input_tokens or est_in, "tokens_out": spent.output_tokens,
                       "tokens_estimated": not spent.input_tokens, "fields_written": written, "fields_rejected": rejected,
                       "output": {k: data.get(k) for k in ("fiscal_year_end", *FIELDS)}})
            e.update(status=DONE, report=ref.to_dict(), attempts=0, last_error=None, retry_after=None, last_success=day, processing_since=None)
            e["analyzed"][ref.version] = {"analyzed_at": day, "fy_end": fy, "fields": written, "outcome": outcome}
            rep.processed.append(s)
        except _Stop as st:
            e.update(status=prev, processing_since=None)       # not a failure: put it back untouched, no attempt counted
            q.save(); rep.stopped = st.reason; break
        except LLMOutputError as exc:
            spent = exc.usage; _fail(e, f"unusable AI output: {exc}", cfg, day, rep, s)
        except _Fail as exc:
            _fail(e, str(exc), cfg, day, rep, s)
        except (DataUnavailableError, ProviderError) as exc:
            _fail(e, f"report download: {exc}", cfg, day, rep, s)
        # ---- usage + checkpoint after EVERY stock
        today["stocks"] += 1
        today["tokens_in"] += spent.input_tokens or (est_in if called else 0)
        today["tokens_out"] += spent.output_tokens
        today["requests"] += 1 if called else 0
        frac = (today["tokens_in"] + today["tokens_out"]) / budget
        if frac >= cfg.warn_at_fraction and not any("daily budget" in a for a in today["alerts"]):
            today["alerts"].append(f"token usage reached {frac:.0%} of the daily budget")
        q.save(); u.save()

    if rep.stopped is None:
        rep.stopped = "completed"
    if rep.stopped != "completed":
        today["alerts"].append(f"batch stopped early: {rep.stopped}")
    today["stopped"] = rep.stopped
    u.save()
    rep.tokens_in, rep.tokens_out, rep.requests, rep.alerts = today["tokens_in"], today["tokens_out"], today["requests"], list(today["alerts"])
    rep.remaining = sum(1 for s in q.active() if q.entries[s]["status"] in (PENDING, FAILED, UNCHECKED, PROCESSING))
    write_dashboard(store_dir, q, u, cfg, now)
    return rep


def _fail(e: dict, msg: str, cfg: AnnualReportConfig, day: str, rep: BatchReport, sym: str) -> None:
    e["attempts"] += 1
    final = e["attempts"] >= cfg.max_attempts
    delay = cfg.retry_days[min(e["attempts"] - 1, len(cfg.retry_days) - 1)]
    e.update(status=FAILED_FINAL if final else FAILED, last_error=msg, processing_since=None,
             retry_after=None if final else (datetime.strptime(day, "%Y-%m-%d") + timedelta(days=delay)).strftime("%Y-%m-%d"))
    rep.failed[sym] = msg
