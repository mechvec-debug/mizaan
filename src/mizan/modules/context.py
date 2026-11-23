"""RunContext: everything a module needs to run, in one object (injectable for tests, live by default)."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ..core.config import load_config
from ..core.errors import ConfigError
from ..core.paths import project_root, resolve
from ..universe import UniverseService


@dataclass
class RunContext:
    cfg: dict
    store: Path
    universe_service: UniverseService
    root: Path = field(default_factory=project_root)
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    dm_factory: Callable | None = None
    xbrl_provider_factory: Callable | None = None
    report_provider_factory: Callable | None = None
    llm_factory: Callable | None = None
    registry: object | None = None
    _dm: object | None = None

    @classmethod
    def live(cls, cfg: dict | None = None) -> "RunContext":
        cfg = cfg or load_config()
        return cls(cfg=cfg, store=resolve(cfg["paths"]["store"]), universe_service=UniverseService())

    # ---- paths
    def now(self) -> datetime:
        return self.clock()

    def results_dir(self, module_id: str) -> Path:
        return self.store / "results" / module_id

    # ---- data
    def universe(self):
        return self.universe_service.get()

    def tickers(self, module_id: str) -> list[str]:
        """The module's stocks: the whole universe, or only Shariah-compliant ones when its universe_filter says so."""
        snap = self.universe()
        spec = self.registry.get(module_id) if self.registry else None
        if not spec or spec.universe_filter == "all":
            return list(snap.tickers)
        try:
            data = json.loads((self.results_dir("shariah") / "latest_results.json").read_text(encoding="utf-8")).get("data", [])
        except (OSError, ValueError):
            raise ConfigError(f"module '{module_id}' is limited to Shariah-compliant stocks: run the Shariah screener first")
        ok = {r["ticker"] for r in data if (r.get("verdict") or ("COMPLIANT" if r.get("is_compliant") else "")) == "COMPLIANT"}
        return list(snap.subset(ok))

    def data_manager(self):
        if self._dm is None:
            if self.dm_factory:
                self._dm = self.dm_factory()
            else:
                from ..data import DataManager
                from ..data.providers.yahoo import YahooProvider
                self._dm = DataManager(YahooProvider(self.cfg["data"]["throttle"].get("timeout_seconds", 30)), self.store, self.cfg["data"])
        return self._dm

    def mapper(self):
        from ..screeners.shariah.mapper import BseMapper
        return BseMapper(self.root / "data" / "universe" / "nse_bse_map.csv")

    def evidence(self):
        from ..screeners.shariah.evidence import EvidenceStore
        return EvidenceStore(self.store / "state" / "shariah_evidence")

    def xbrl_provider(self):
        if self.xbrl_provider_factory:
            return self.xbrl_provider_factory()
        from ..data.providers.bse import BseXbrlProvider
        from ..screeners.shariah.xbrl import parse_xbrl_facts
        return BseXbrlProvider(parse_xbrl_facts)

    def report_provider(self):
        if self.report_provider_factory:
            return self.report_provider_factory()
        from ..annual_report.reports import BseReportProvider
        from ..data.providers.bse import BseXbrlProvider
        from ..screeners.shariah.xbrl import parse_xbrl_facts
        return BseReportProvider(BseXbrlProvider(parse_xbrl_facts), parse_xbrl_facts)

    def llm(self):
        if self.llm_factory:
            return self.llm_factory()
        from ..annual_report.config import AnnualReportConfig
        from ..annual_report.llm import GeminiClient
        return GeminiClient(model=AnnualReportConfig.from_config(self.cfg).model)
