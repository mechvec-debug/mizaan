"""DataManager - the ONLY door to external market / fundamental data (requirement 5).

Screeners call get_ohlcv / get_info / get_statements. The manager guarantees:
  * one shared cache (data_store/raw) -> a download by one screener is reused by all others
  * single-flight: concurrent requests for the same key trigger ONE download (threads and processes)
  * incremental updates: only new bars are fetched; a longer history request fetches only the missing earlier part
  * adjustment safety: a dividend/split/bonus that revises history is detected on the overlap and triggers a refresh
  * never silent: results carry status (fresh|stale|partial) and issues; failures raise DataUnavailableError
  * throttling, retry/back-off, circuit breaker, and a download ledger for every provider call
Raw data is returned as COPIES - a screener cannot modify the shared store.
"""
from __future__ import annotations

import copy
import logging
import random
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import pandas as pd

from ..core.errors import DataUnavailableError, PermanentProviderError, ProviderPausedError, RateLimitError, TransientProviderError
from . import ohlcv as ox
from .keys import CacheKey
from .ledger import DownloadLedger
from .locks import FileLockTimeout, KeyedLocks, file_lock
from .store import RawStore, df_to_obj, obj_to_df
from .throttle import CircuitBreaker, RateLimiter, retry_call

log = logging.getLogger("mizan.data")

DEFAULTS = {
    "cache_freshness_hours": {"ohlcv": 20, "info": 20, "statements": 168, "xbrl": 8760, "xbrl_missing": 168},
    "ohlcv": {"initial_history_years": 10, "overlap_days": 7, "drift_tolerance": 1e-4, "max_bar_age_days": 7,
              "history_complete_tolerance_days": 7},
    "throttle": {"requests_per_minute": 30, "max_retries": 3, "base_delay_seconds": 2.0, "rate_limit_delay_seconds": 20.0,
                 "breaker_threshold": 3, "batch_size": 50, "lock_timeout_seconds": 120,
                 # one provider CALL can be several HTTP requests: yfinance statements = 3, info (+fast_info) = 2, a BSE filing lookup = pages + downloads
                 "weights": {"ohlcv": 1, "info": 2, "statements": 3, "xbrl": 6}, "rpm_by_source": {}},
}


def _merge_cfg(base: dict, over: dict | None) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = _merge_cfg(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


@dataclass
class DataResult:
    data: object
    status: str                       # fresh | stale | partial
    key: str
    source: str
    fetched_at: str
    issues: list = field(default_factory=list)
    from_cache: bool = True
    actions: list = field(default_factory=list)       # provider calls made to satisfy THIS request
    first_date: str | None = None
    last_date: str | None = None
    rows: int = 0

    @property
    def ok(self) -> bool:
        return self.status == "fresh"


@dataclass
class PrefetchReport:
    requested: int = 0
    cache_hits: int = 0
    downloaded: list = field(default_factory=list)
    failed: dict = field(default_factory=dict)
    deferred: list = field(default_factory=list)      # not attempted because the circuit breaker opened


class DataManager:
    def __init__(self, provider, store_root, config: dict | None = None, *, run_id: str | None = None,
                 clock: Callable[[], datetime] | None = None, sleep: Callable[[float], None] = time.sleep,
                 rng: Callable[[], float] = random.random):
        self.provider = provider
        self.cfg = _merge_cfg(DEFAULTS, (config or {}).get("data", config) if config and "data" in config else config)
        self.root = Path(store_root)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._sleep, self._rng = sleep, rng
        self.run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
        self.store = RawStore(self.root / "raw")
        self.ledger = DownloadLedger(self.root / "ledger", self.run_id, self._clock)
        t = self.cfg["throttle"]
        self._sleep_fn, self._limiters = sleep, {}
        self.limiter = self._limiter(provider.name)
        self.breaker = CircuitBreaker(t["breaker_threshold"])
        self._locks = KeyedLocks()
        self._lock_dir = self.root / "locks"
        self._failed: dict[str, str] = {}
        self.deadline: datetime | None = None        # run-time budget: no NEW download is started after this moment

    # ================================================================= helpers
    def _limiter(self, source: str) -> RateLimiter:
        """One request budget per external source (Yahoo and BSE are different services with different limits)."""
        if source not in self._limiters:
            t = self.cfg["throttle"]
            self._limiters[source] = RateLimiter(t["rpm_by_source"].get(source, t["requests_per_minute"]), sleep=self._sleep_fn)
        return self._limiters[source]

    def _paused(self) -> str | None:
        """Why no new request may start right now: the circuit breaker is open, or the run-time budget is used up."""
        if self.breaker.tripped:
            return "provider paused after repeated rate limiting"
        if self.deadline is not None and self._now() >= self.deadline:
            return "run time budget reached"
        return None

    def _now(self) -> datetime:
        return self._clock()

    @staticmethod
    def _iso(dt: datetime) -> str:
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    @staticmethod
    def _age_hours(iso: str, now: datetime) -> float:
        return (now - pd.Timestamp(iso).to_pydatetime()).total_seconds() / 3600.0

    def _ttl(self, data_type: str) -> float:
        return float(self.cfg["cache_freshness_hours"][data_type])

    @contextmanager
    def _guard(self, key: CacheKey):
        try:
            with self._locks.get(str(key)):
                with file_lock(self._lock_dir / f"{key.digest}.lock", timeout=self.cfg["throttle"]["lock_timeout_seconds"]):
                    yield
        except FileLockTimeout as exc:
            raise DataUnavailableError(f"{key}: {exc}", key.ticker, key.data_type, "lock_timeout") from exc

    def _download(self, key: CacheKey, action: str, fn: Callable, *, memo: bool = True, weight: int | None = None):
        """One provider call with throttling + retry + breaker + ledger. Returns the provider value."""
        base = dict(ticker=key.ticker, data_type=key.data_type, source=key.source, period=key.period, action=action)
        memo_key = f"{key}#{action}"
        if memo and memo_key in self._failed:      # do not hammer a source that already failed in this run
            raise DataUnavailableError(f"{key}: failed earlier in this run ({self._failed[memo_key]})", key.ticker, key.data_type, "earlier_failure")
        why = self._paused()
        if why:
            self.ledger.record(**base, status="skipped", error=why)
            raise ProviderPausedError(f"{key}: {why}", key.ticker, key.data_type, "provider_paused")
        t = self.cfg["throttle"]
        t0 = time.monotonic()

        w = weight if weight is not None else t["weights"].get(key.data_type, 1)

        def call():
            self._limiter(key.source).acquire(w)
            return fn()

        try:
            value, attempts = retry_call(call, retries=t["max_retries"], base_delay=t["base_delay_seconds"],
                                         rate_limit_delay=t["rate_limit_delay_seconds"], sleep=self._sleep, rng=self._rng)
        except (RateLimitError, PermanentProviderError, TransientProviderError) as exc:
            reason = {RateLimitError: "rate_limited", PermanentProviderError: "permanent"}.get(type(exc), "transient")
            if isinstance(exc, RateLimitError):
                self.breaker.record_rate_limit()
            self.ledger.record(**base, status="failed", error=f"{reason}: {exc}", duration_ms=(time.monotonic() - t0) * 1000)
            if memo:
                self._failed[memo_key] = reason
            raise DataUnavailableError(f"{key}: {reason}: {exc}", key.ticker, key.data_type, reason) from exc
        self.breaker.record_success()
        rows = len(value) if hasattr(value, "__len__") else 1
        self.ledger.record(**base, rows=rows, status="ok", duration_ms=(time.monotonic() - t0) * 1000, attempts=attempts)
        return value

    # ================================================================= OHLCV
    def get_ohlcv(self, ticker: str, *, years: float | None = None, min_bars: int | None = None,
                  adjusted: bool = True, allow_stale: bool = False) -> DataResult:
        """Daily bars for `ticker`. years=None -> everything stored. adjusted=True -> split+dividend adjusted view
        (what the original screeners used); False -> raw stored columns."""
        key = CacheKey.make(ticker, "ohlcv", self.provider.name, "1d")
        psym = self.provider.provider_symbol(ticker)
        o = self.cfg["ohlcv"]
        now = self._now()
        today = pd.Timestamp(now.date())
        need_start = (today - pd.DateOffset(years=years)) if years else None
        tol = pd.Timedelta(days=o["history_complete_tolerance_days"])
        actions, issues, stale = [], [], False
        with self._guard(key):
            raw, meta = self.store.read_ohlcv(key)             # read AFTER acquiring the lock (double-check)
            try:
                if raw is None:
                    span = max(years or 0, o["initial_history_years"])
                    start = (today - pd.DateOffset(years=span)).date()
                    raw, meta = self._full(key, psym, start, now, "full", prior=None)
                    actions.append("full")
                else:
                    if self._age_hours(meta["fetched_at"], now) > self._ttl("ohlcv"):
                        raw, meta, act = self._incremental(key, psym, raw, meta, now)
                        actions.append(act)
                    if need_start is not None and not meta.get("history_complete") and need_start < raw.index[0] - tol:
                        raw, meta, act = self._backfill(key, psym, raw, meta, need_start, now)
                        actions.append(act)
            except DataUnavailableError as exc:
                if raw is not None and allow_stale:
                    issues.append(f"served stale cache because refresh failed ({exc.reason})")
                    stale = True
                else:
                    raise
        return self._ohlcv_result(key, raw, meta, now, need_start, min_bars, adjusted, actions, issues, stale)

    def _ohlcv_result(self, key, raw, meta, now, need_start, min_bars, adjusted, actions, issues, stale) -> DataResult:
        status = "stale" if stale else "fresh"
        view = raw if need_start is None else raw[raw.index >= need_start]
        age_days = (now.date() - raw.index[-1].date()).days
        if age_days > self.cfg["ohlcv"]["max_bar_age_days"]:
            status = "stale"
            issues.append(f"last bar {raw.index[-1]:%Y-%m-%d} is {age_days} days old (suspended or delisted?)")
        if need_start is not None and len(view) and view.index[0] > need_start + pd.Timedelta(days=self.cfg["ohlcv"]["history_complete_tolerance_days"]):
            issues.append(f"history starts {view.index[0]:%Y-%m-%d}, later than the requested window start (recent listing)")
        if min_bars is not None and len(view) < min_bars:
            status = "stale" if status == "stale" else "partial"
            issues.append(f"only {len(view)} bars available, {min_bars} required")
        issues += ox.validate(view)
        if not actions:
            self.ledger.cache_hit("ohlcv")
        data = ox.to_adjusted(view) if adjusted else view.copy()
        return DataResult(data=data, status=status, key=str(key), source=key.source, fetched_at=meta["fetched_at"], issues=issues,
                          from_cache=not actions, actions=actions, first_date=meta["first_date"], last_date=meta["last_date"], rows=len(view))

    # -- download paths --------------------------------------------------
    def _store_full(self, key, psym, df, start, now, prior):
        new = ox.clean_raw(df)
        if new.empty:
            self._failed[f"{key}#full"] = "no_data"
            raise DataUnavailableError(f"{key}: provider returned no data", key.ticker, key.data_type, "no_data")
        tol = pd.Timedelta(days=self.cfg["ohlcv"]["history_complete_tolerance_days"])
        complete = bool((prior or {}).get("history_complete")) or new.index[0] > pd.Timestamp(start) + tol
        meta = {"symbol": key.ticker, "source": key.source, "provider_symbol": psym, "fetched_at": self._iso(now),
                "history_complete": complete, "last_full_refresh_at": self._iso(now)}
        self.store.write_ohlcv(key, new, meta)
        return self.store.read_ohlcv(key)

    def _full(self, key, psym, start, now, action, prior):
        df = self._download(key, action, lambda: self.provider.fetch_ohlcv(psym, start, None))
        return self._store_full(key, psym, df, start, now, prior)

    def _incremental(self, key, psym, raw, meta, now):
        start = (raw.index[-1] - pd.Timedelta(days=self.cfg["ohlcv"]["overlap_days"])).date()
        df = self._download(key, "incremental", lambda: self.provider.fetch_ohlcv(psym, start, None))
        return self._apply_incremental(key, psym, raw, meta, ox.clean_raw(df), now)

    def _apply_incremental(self, key, psym, raw, meta, new, now):
        if new.empty:                                           # holiday / no new bar: just record that we checked
            meta = {**meta, "fetched_at": self._iso(now)}
            self.store.write_meta(key, meta)
            return raw, meta, "incremental"
        d = ox.drift(raw, new)
        if d > self.cfg["ohlcv"]["drift_tolerance"]:
            log.warning("%s: provider history changed (drift %.4f) - refreshing full history", key.ticker, d)
            raw2, meta2 = self._full(key, psym, raw.index[0].date(), now, "full_refresh", prior=meta)
            return raw2, meta2, "full_refresh"
        merged = ox.merge(raw, new)
        meta = {**meta, "fetched_at": self._iso(now)}
        self.store.write_ohlcv(key, merged, meta)
        raw2, meta2 = self.store.read_ohlcv(key)
        return raw2, meta2, "incremental"

    def _backfill(self, key, psym, raw, meta, need_start, now):
        o = self.cfg["ohlcv"]
        end = (raw.index[0] + pd.Timedelta(days=o["overlap_days"])).date()
        df = self._download(key, "backfill", lambda: self.provider.fetch_ohlcv(psym, need_start.date(), end))
        new = ox.clean_raw(df)
        tol = pd.Timedelta(days=o["history_complete_tolerance_days"])
        if new.empty or new.index[0] >= raw.index[0]:           # nothing earlier exists at the provider
            meta = {**meta, "history_complete": True}
            self.store.write_meta(key, meta)
            return raw, meta, "backfill"
        if ox.drift(raw, new) > o["drift_tolerance"]:
            raw2, meta2 = self._full(key, psym, need_start.date(), now, "full_refresh", prior=meta)
            return raw2, meta2, "full_refresh"
        complete = bool(new.index[0] > need_start + tol)
        self.store.write_ohlcv(key, ox.merge(raw, new), {**meta, "history_complete": complete or meta.get("history_complete", False)})
        raw2, meta2 = self.store.read_ohlcv(key)
        return raw2, meta2, "backfill"

    # -- batch prefetch (one HTTP call for many tickers) -------------------
    def _classify(self, key, now, years):
        raw, meta = self.store.read_ohlcv(key)
        if raw is None:
            return "full", None, None
        tol = pd.Timedelta(days=self.cfg["ohlcv"]["history_complete_tolerance_days"])
        need_start = (pd.Timestamp(now.date()) - pd.DateOffset(years=years)) if years else None
        stale = self._age_hours(meta["fetched_at"], now) > self._ttl("ohlcv")
        backfill = need_start is not None and not meta.get("history_complete") and need_start < raw.index[0] - tol
        if backfill:
            return "single", raw, meta
        return ("incremental" if stale else "hit"), raw, meta

    def prefetch_ohlcv(self, tickers: list[str], *, years: float | None = None) -> PrefetchReport:
        """Warm the cache with as few provider calls as possible (batch endpoint). get_ohlcv() afterwards is a cache hit."""
        rep, now = PrefetchReport(requested=len(tickers)), self._now()
        o, bs = self.cfg["ohlcv"], self.cfg["throttle"]["batch_size"]
        groups: dict[str, list] = {"full": [], "incremental": []}
        for t in dict.fromkeys(tickers):
            key = CacheKey.make(t, "ohlcv", self.provider.name, "1d")
            kind, raw, meta = self._classify(key, now, years)
            if kind == "hit":
                rep.cache_hits += 1; self.ledger.cache_hit("ohlcv")
            elif kind in groups:
                groups[kind].append((t, key, raw))
        for kind, items in groups.items():
            for i in range(0, len(items), bs):
                chunk = items[i:i + bs]
                if self._paused():
                    rep.deferred += [t for t, _, _ in chunk]; continue
                if kind == "full":
                    span = max(years or 0, o["initial_history_years"])
                    start = (pd.Timestamp(now.date()) - pd.DateOffset(years=span)).date()
                else:
                    start = min((r.index[-1] for _, _, r in chunk)) - pd.Timedelta(days=o["overlap_days"])
                    start = start.date()
                psyms = {t: self.provider.provider_symbol(t) for t, _, _ in chunk}
                try:
                    res = self._download(chunk[0][1], f"{kind}_batch", lambda: self.provider.fetch_ohlcv_batch(list(psyms.values()), start, None), memo=False, weight=len(chunk))
                except DataUnavailableError as exc:
                    for t, _, _ in chunk:
                        rep.failed[t] = exc.reason
                    continue
                for t, key, _ in chunk:
                    got = res.get(psyms[t])
                    base = dict(ticker=t, data_type="ohlcv", source=key.source, period="1d", action=f"{kind}_batch_item")
                    if isinstance(got, Exception) or got is None:
                        rep.failed[t] = type(got).__name__ if got is not None else "missing"
                        self.ledger.record(**base, status="failed", error=str(got)); continue
                    try:
                        with self._guard(key):
                            raw, meta = self.store.read_ohlcv(key)
                            if kind == "full" or raw is None:
                                self._store_full(key, psyms[t], got, start, now, None)
                            else:
                                self._apply_incremental(key, psyms[t], raw, meta, ox.clean_raw(got), now)
                        self.ledger.record(**base, rows=len(got), status="ok")
                        rep.downloaded.append(t)
                    except DataUnavailableError as exc:
                        rep.failed[t] = exc.reason
        return rep

    # ================================================================= info / statements
    def _blob_get(self, ticker: str, data_type: str, period: str, fetch: Callable, pack: Callable, unpack: Callable,
                  allow_stale: bool, provider=None, psym: str | None = None, ttl_fn: Callable | None = None) -> DataResult:
        """Shared JSON-blob cache. `provider`/`psym` let another source (e.g. BSE) use the same cache, ledger, retry and breaker;
        `ttl_fn(data) -> hours` lets a source expire 'not found' answers sooner than real data."""
        prov = provider or self.provider
        key = CacheKey.make(ticker, data_type, prov.name, period)
        psym, now, issues, actions, stale = (psym if psym is not None else prov.provider_symbol(ticker)), self._now(), [], [], False
        with self._guard(key):
            blob = self.store.read_blob(key)
            ttl = (ttl_fn(blob["data"]) if (blob is not None and ttl_fn) else self._ttl(data_type))
            if blob is None or self._age_hours(blob["fetched_at"], now) > ttl:
                act = "refresh" if blob else "full"
                try:
                    value = self._download(key, act, lambda: fetch(psym))
                    self.store.write_blob(key, self._iso(now), pack(value))
                    blob, actions = self.store.read_blob(key), [act]
                except DataUnavailableError as exc:
                    if blob is not None and allow_stale:
                        issues.append(f"served stale cache because refresh failed ({exc.reason})"); stale = True
                    else:
                        raise
        if not actions:
            self.ledger.cache_hit(data_type)
        return DataResult(data=unpack(blob["data"]), status="stale" if stale else "fresh", key=str(key), source=key.source,
                          fetched_at=blob["fetched_at"], issues=issues, from_cache=not actions, actions=actions)

    def get_info(self, ticker: str, *, allow_stale: bool = False) -> DataResult:
        """Yahoo 'info' dict (market cap, PE, sector ...). Changes daily -> short TTL."""
        return self._blob_get(ticker, "info", "snapshot", self.provider.fetch_info, lambda d: d, copy.deepcopy, allow_stale)

    def get_statements(self, ticker: str, *, allow_stale: bool = False) -> DataResult:
        """{'income_stmt','balance_sheet','financials'} -> DataFrame|None. Changes quarterly -> long TTL."""
        pack = lambda d: {k: df_to_obj(v) for k, v in d.items()}
        unpack = lambda d: {k: obj_to_df(v) for k, v in d.items()}
        return self._blob_get(ticker, "statements", "annual", self.provider.fetch_statements, pack, unpack, allow_stale)

    def get_xbrl_facts(self, ticker: str, bse_code: str, provider, *, allow_stale: bool = False) -> DataResult:
        """Extracted facts of the latest annual XBRL filing ({'found': bool, ...}). A real filing is cached ~1 year; a 'not found'
        answer is re-checked after `xbrl_missing` hours (default 7 days). Raw XML is never stored."""
        hit, miss = self._ttl("xbrl"), self._ttl("xbrl_missing")
        return self._blob_get(ticker, "xbrl", "annual", provider.fetch_xbrl_facts, lambda d: d, copy.deepcopy, allow_stale,
                              provider=provider, psym=str(bse_code), ttl_fn=lambda data: hit if data.get("found") else miss)

    # ================================================================= reporting
    def stats(self) -> dict:
        return {"run_id": self.run_id, "downloads": dict(self.ledger.downloads), "cache_hits": dict(self.ledger.hits),
                "failures": dict(self.ledger.failures), "breaker_tripped": self.breaker.tripped, "deadline_reached": self._paused() == "run time budget reached",
                "failed_keys": dict(self._failed), "store": self.store.size_report()}

    def close(self) -> Path:
        return self.ledger.flush_run_stats()
