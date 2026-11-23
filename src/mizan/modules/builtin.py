"""The four built-in modules, registered through the SAME mechanism a custom module uses. The adapters only call the existing
engines' public functions - no engine is touched. Entry points are strings, so nothing heavy is imported until a module runs."""
from __future__ import annotations

import json
from dataclasses import asdict

from .registry import ModuleSpec, Registry

P = "mizan.dashboard.pages:"


def _prev(path, key=None):
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return d.get(key) if key and isinstance(d, dict) else d
    except (OSError, ValueError):
        return None


def run_technical(ctx) -> dict:
    from ..screeners.technical.config import load_technical_config
    from ..screeners.technical.scanner import scan_universe, write_outputs
    out = ctx.results_dir("technical")
    res = scan_universe(ctx.tickers("technical"), ctx.data_manager(), load_technical_config(), now=ctx.now(), previous=_prev(out / "trading_alerts.json"))
    write_outputs(res, out, {"universe_version": ctx.universe().version})
    return dict(res.meta)


def run_fundamental(ctx) -> dict:
    from ..screeners import fundamental as f
    u = ctx.universe()
    sub = ctx.tickers("fundamental")
    snap = u if len(sub) == u.count else type("U", (), {"tickers": sub, "version": u.version, "count": len(sub)})()
    return f.run(snap, ctx.data_manager(), ctx.results_dir("fundamental"), f.FundamentalParams(), now=ctx.now())


def run_shariah(ctx) -> dict:
    from ..screeners import shariah as s
    return s.run(ctx.universe(), ctx.data_manager(), ctx.results_dir("shariah"), ctx.mapper(), ctx.evidence(), s.ShariahParams(),
                 xbrl_provider=ctx.xbrl_provider(), now=ctx.now())


def run_annual_report(ctx) -> dict:
    from ..annual_report import AnnualReportConfig, run_batch
    cfg = AnnualReportConfig.from_config(ctx.cfg)
    cfg.require_limits()                       # ConfigError -> the registry reports 'not_configured' (not an error)
    rep = run_batch(ctx.universe().tickers, store_dir=ctx.store, provider=ctx.report_provider(), llm=ctx.llm(), evidence=ctx.evidence(),
                    dm=ctx.data_manager(), mapper=ctx.mapper(), cfg=cfg, now=ctx.now())
    return {k: v for k, v in asdict(rep).items() if k in ("date", "stopped", "discovered", "tokens_in", "tokens_out", "requests", "remaining", "alerts")} | \
           {"processed": len(rep.processed), "failed": len(rep.failed)}


def register_builtins(reg: Registry) -> Registry:
    E = "mizan.modules.builtin:"
    reg.register(ModuleSpec("technical", "Swing Setup Radar", "EMA-cross entries inside Fibonacci pullback zones (retracement trades).", "screener",
                            data_dependencies=("ohlcv",), run=E + "run_technical", page=P + "render_technical", order=10, builtin=True,
                            output_schema={"results_file": "trading_alerts.json", "required_columns": ["ticker", "action", "entry", "sl", "tp1", "tp2", "tp3", "aligned", "plan", "time_received"]}))
    reg.register(ModuleSpec("fundamental", "Quality Value Rebound", "Strong businesses at a fair price, well off their highs, with the chart turning up.", "screener",
                            data_dependencies=("ohlcv", "info", "statements"), run=E + "run_fundamental", page=P + "render_fundamental", order=20, builtin=True,
                            output_schema={"results_file": "data.json", "required_columns": ["Ticker", "Status", "Sector", "Current price"]}))
    reg.register(ModuleSpec("shariah", "Shariah Compliance Screener", "Six-criteria Shariah screen (strict: unknown stays undetermined).", "screener",
                            data_dependencies=("info", "statements", "xbrl"), run=E + "run_shariah", page=P + "render_shariah", order=30, builtin=True,
                            output_schema={"results_file": "latest_results.json", "rows_key": "data", "required_columns": ["ticker", "metrics", "is_compliant"]}))
    reg.register(ModuleSpec("annual_report", "Annual Report Intelligence", "Controlled daily AI reading of annual reports; fills the Shariah evidence.", "analysis",
                            inputs=("universe", "shariah evidence store"), data_dependencies=("info", "xbrl"), run=E + "run_annual_report",
                            page=P + "render_annual_report", order=40, builtin=True,
                            output_schema={"results_file": "dashboard.json"}))
    return reg


def build_registry(cfg: dict, modules_yaml: list | None = None, state_path=None) -> Registry:
    reg = register_builtins(Registry(state_path))
    reg.apply_config(modules_yaml or [])
    return reg
