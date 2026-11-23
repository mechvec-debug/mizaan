"""Annual Report Intelligence (Phase 8): controlled, resumable AI analysis that fills the Shariah evidence store."""
from .config import HARD_STOCK_CAP, AnnualReportConfig
from .dashboard import summary, write_dashboard
from .engine import BatchReport, run_batch
from .llm import GeminiClient, LLMOutputError, Usage
from .reports import BseReportProvider, ReportRef
from .state import AnalysisStore, QueueStore, UsageStore

__all__ = ["AnnualReportConfig", "HARD_STOCK_CAP", "run_batch", "BatchReport", "summary", "write_dashboard", "GeminiClient", "Usage",
           "LLMOutputError", "ReportRef", "BseReportProvider", "QueueStore", "UsageStore", "AnalysisStore"]
