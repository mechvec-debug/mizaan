"""Annual-report AI settings. The batch REFUSES to run until the daily token budget and request rate are set
(from your provider's CURRENT limits) - it must never guess a limit and burn through a free tier."""
from __future__ import annotations

from dataclasses import dataclass, fields
from typing import ClassVar

from ..core.errors import ConfigError

HARD_STOCK_CAP = 100                      # requirement: never more than 100 stocks per scheduled daily batch


@dataclass(frozen=True)
class AnnualReportConfig:
    max_stocks_per_day: int = 100
    daily_token_budget: int | None = None
    requests_per_minute: float | None = None
    warn_at_fraction: float = 0.8
    model: str = "gemini-2.5-flash"                 # the original auditor's model
    max_output_tokens: int = 1024
    max_input_chars: int = 32000                    # only selected note sections are ever sent, never a whole report
    chars_per_token: float = 3.0                    # conservative estimate used BEFORE a call to protect the budget
    max_pages: int = 8                              # same cap as the original page extractor
    discovery_per_day: int = 200                    # BSE filing look-ups per day (separate from the AI budget)
    discovery_requests_per_minute: float = 20.0
    max_attempts: int = 5
    retry_days: tuple = (1, 3, 7, 14)
    recheck_missing_days: int = 30                  # a stock with no report is looked up again after this long
    recheck_done_days: int = 300                    # an analysed stock is checked for a NEW annual report after this long
    processing_timeout_hours: float = 6.0           # a 'processing' entry older than this is treated as a crashed run
    llm_retries: int = 2
    schedule_hour_utc: int = 11                     # only used to show the next scheduled batch
    allow_pdf_fallback: bool = True                 # PDF is a workaround for filings without usable XML text

    @property
    def stock_cap(self) -> int:
        return max(0, min(int(self.max_stocks_per_day), HARD_STOCK_CAP))

    def require_limits(self) -> None:
        missing = [k for k in ("daily_token_budget", "requests_per_minute") if getattr(self, k) in (None, 0)]
        if missing:
            raise ConfigError("annual_report." + " and annual_report.".join(missing) + " must be set in config/app.yaml from your AI "
                              "provider's current limits before the batch can run (it never guesses a limit).")

    @classmethod
    def from_config(cls, cfg: dict) -> "AnnualReportConfig":
        raw = (cfg or {}).get("annual_report", {}) or {}
        names = {f.name for f in fields(cls)}
        kw = {k: v for k, v in raw.items() if k in names}
        if "retry_days" in kw:
            kw["retry_days"] = tuple(kw["retry_days"])
        return cls(**kw)
