"""Builds a real RunContext (registry + all four built-in modules) on synthetic data - no network."""
import sys
from collections import Counter
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from fakes import FakeClock, FakeProvider  # noqa: E402
from mizan.core.auth import Session  # noqa: E402
from mizan.dashboard.app import build_app  # noqa: E402
from mizan.data import DataManager  # noqa: E402
from mizan.modules.context import RunContext  # noqa: E402
from mizan.universe import UniverseService  # noqa: E402

CR = 1e7
ADMIN = Session("mechvec@gmail.com", "Sami", "admin")
GOOD = {"Total Assets": 1000, "Total Liabilities Net Minority Interest": 400, "Total Debt": 100, "Net PPE": 300, "Inventory": 100,
        "Cash Cash Equivalents And Short Term Investments": 150, "Receivables": 100, "Current Assets": 400}
PNL = {"Total Revenue": 800, "Interest Income": 8}


def _st(bs, pnl):
    c, p = pd.Timestamp("2026-03-31"), pd.Timestamp("2025-03-31")
    f = lambda r: pd.DataFrame({c: {k: v * CR for k, v in r.items()}, p: {k: v * CR for k, v in r.items()}})[[c, p]]
    return {"balance_sheet": f(bs), "income_stmt": f(pnl), "financials": None}


class DemoProvider(FakeProvider):
    S = {"AAA": _st(GOOD, PNL), "BBB": _st({**GOOD, "Total Debt": 450}, PNL), "CCC": _st(GOOD, PNL),
         "BANK": _st({"Total Assets": 10000, "Total Liabilities Net Minority Interest": 9000, "Total Debt": 500}, {"Total Revenue": 1000, "Interest Income": 700}),
         "DDD": _st({**GOOD, "Net PPE": 10, "Inventory": 5}, PNL)}

    def fetch_statements(self, s):
        self.stmt_calls += 1
        return self.S[s[:-3]]


class NoFilingXbrl:
    name = "bsefake"

    def __init__(self):
        self.calls = Counter()

    def provider_symbol(self, t):
        return t

    def fetch_xbrl_facts(self, code):
        self.calls[code] += 1
        return {"found": False, "checked_at": "x"}


def make_ctx(tmp: Path, names=("AAA", "BBB", "CCC", "BANK", "DDD"), cfg_extra=None):
    prov = DemoProvider()
    for i, n in enumerate(names):
        prov.add(n, seed=30 + i)
    (tmp / "u.txt").write_text("\n".join(names) + "\n", encoding="utf-8")
    store, clock = tmp / "store", FakeClock()
    cfg = {"admin": {"email": "mechvec@gmail.com", "name": "Sami"}, "annual_report": {}, **(cfg_extra or {})}
    dm = DataManager(prov, store, {"throttle": {"requests_per_minute": 0, "max_retries": 0}}, clock=clock, sleep=lambda s: None)
    ctx = RunContext(cfg=cfg, store=store, universe_service=UniverseService(tmp / "u.txt"), root=ROOT, clock=clock,
                     dm_factory=lambda: dm, xbrl_provider_factory=NoFilingXbrl)
    ctx, auth = build_app(cfg, ctx=ctx)
    return ctx, auth, prov
