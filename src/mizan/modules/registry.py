"""Module registry: how screeners and analyses plug into the platform WITHOUT editing any existing engine.

A module declares: id, name, description, category, input requirements, data dependencies, an execution function, an output schema,
a presentation component and an enabled flag. The registry validates the declaration, isolates failures (one module's crash never
touches another, never raises into the caller) and builds the dashboard navigation - an enabled custom module appears automatically.
"""
from __future__ import annotations

import importlib
import json
import re
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ..core.errors import ConfigError

CATEGORIES = ("screener", "analysis", "custom_screener", "custom_analysis")
ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")


@dataclass
class ModuleSpec:
    id: str
    name: str
    description: str = ""
    category: str = "custom_screener"
    inputs: tuple = ("universe",)                       # what it needs: the stock universe, other modules' results ...
    data_dependencies: tuple = ()                       # which Data Manager data it reads: ohlcv / info / statements / xbrl
    run: Callable | str | None = None                   # fn(ctx) -> dict summary   (or "package.module:function")
    output_schema: dict = field(default_factory=dict)   # {"results_file": "x.json", "rows_key": "data", "required_columns": [...]}
    page: Callable | str | None = None                  # fn(st, ctx, session, spec) renders it (default: generic table page)
    enabled: bool = True
    universe_filter: str = "all"                        # all | shariah_compliant
    order: int = 100
    builtin: bool = False


@dataclass
class NavEntry:
    key: str
    label: str
    module_id: str | None = None
    section: str = "core"


@dataclass
class RunOutcome:
    module_id: str
    status: str                       # ok | error | not_configured | disabled | invalid | unknown
    started_at: str = ""
    seconds: float = 0.0
    summary: dict = field(default_factory=dict)
    error: str | None = None
    output_problems: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def resolve(ref):
    """'pkg.mod:attr' -> object; callables pass through; None -> None."""
    if ref is None or callable(ref):
        return ref
    mod, _, attr = str(ref).partition(":")
    return getattr(importlib.import_module(mod), attr)


def validate_spec(spec: ModuleSpec) -> list[str]:
    p = []
    if not ID_RE.match(spec.id or ""):
        p.append("id must be lower-case letters, digits or '_' (2-41 chars, starting with a letter)")
    if not (spec.name or "").strip():
        p.append("name is required")
    if spec.category not in CATEGORIES:
        p.append(f"category must be one of {CATEGORIES}")
    if spec.universe_filter not in ("all", "shariah_compliant"):
        p.append("universe_filter must be 'all' or 'shariah_compliant'")
    if not isinstance(spec.output_schema, dict):
        p.append("output_schema must be a mapping")
    elif spec.output_schema and not isinstance(spec.output_schema.get("results_file"), str):
        p.append("output_schema.results_file (a file name inside results/<id>/) is required when an output schema is given")
    if spec.run is None:
        p.append("an execution function (run) is required")
    for what in ("run", "page"):
        ref = getattr(spec, what)
        if ref is None:
            continue
        try:
            if not callable(resolve(ref)):
                p.append(f"{what} is not callable")
        except Exception as exc:
            p.append(f"{what} cannot be loaded ({type(exc).__name__}: {exc})")
    return p


class Registry:
    def __init__(self, state_path: str | Path | None = None):
        self._specs: dict[str, ModuleSpec] = {}
        self.invalid: dict[str, list[str]] = {}
        self.state_path = Path(state_path) if state_path else None
        self._override: dict[str, bool] = {}
        if self.state_path and self.state_path.exists():
            try:
                self._override = json.loads(self.state_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._override = {}

    # ------------------------------------------------------------------ registration
    def register(self, spec: ModuleSpec, replace: bool = False) -> list[str]:
        """Returns the list of problems; an invalid module is NOT registered (and is remembered in .invalid)."""
        problems = validate_spec(spec)
        if spec.id in self._specs and not replace:
            problems.append(f"a module with id '{spec.id}' is already registered")
        if problems:
            self.invalid[spec.id or "?"] = problems
            return problems
        self.invalid.pop(spec.id, None)
        self._specs[spec.id] = spec
        return []

    def apply_config(self, entries: list[dict]) -> None:
        """config/modules.yaml: tune built-ins (name, enabled, universe_filter) or declare a custom module by entrypoint."""
        for e in entries or []:
            mid = e.get("id")
            if mid in self._specs:
                s = self._specs[mid]
                s.name, s.enabled = e.get("display_name", s.name), bool(e.get("enabled", s.enabled))
                s.universe_filter = e.get("universe_filter", s.universe_filter)
                continue
            schema = {}
            if e.get("results_file"):
                schema = {"results_file": e["results_file"], "rows_key": e.get("rows_key"), "required_columns": list(e.get("required_columns", []))}
            self.register(ModuleSpec(id=mid or "", name=e.get("display_name", ""), description=e.get("description", ""),
                                     category=e.get("category", "custom_screener"), inputs=tuple(e.get("inputs", ("universe",))),
                                     data_dependencies=tuple(e.get("data_dependencies", ())), run=e.get("entrypoint"), page=e.get("page"),
                                     output_schema=schema, enabled=bool(e.get("enabled", True)),
                                     universe_filter=e.get("universe_filter", "all"), order=int(e.get("order", 100))))

    # ------------------------------------------------------------------ lookup / state
    def get(self, module_id: str) -> ModuleSpec | None:
        return self._specs.get(module_id)

    def all(self) -> list[ModuleSpec]:
        return sorted(self._specs.values(), key=lambda s: (s.order, s.id))

    def is_enabled(self, module_id: str) -> bool:
        s = self._specs.get(module_id)
        return bool(s) and self._override.get(module_id, s.enabled)

    def enabled(self, category: str | tuple | None = None) -> list[ModuleSpec]:
        cats = (category,) if isinstance(category, str) else category
        return [s for s in self.all() if self.is_enabled(s.id) and (cats is None or s.category in cats)]

    def set_enabled(self, module_id: str, flag: bool) -> None:
        if module_id not in self._specs:
            raise KeyError(module_id)
        self._override[module_id] = bool(flag)
        if self.state_path:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps(self._override, indent=1, sort_keys=True), encoding="utf-8")

    # ------------------------------------------------------------------ navigation
    def navigation(self) -> list[NavEntry]:
        """The ten fixed pages, in the required order; enabled custom modules appear under their section automatically."""
        name = lambda mid, default: (self._specs[mid].name if mid in self._specs else default)
        nav = [NavEntry("overview", "📊 Executive Overview"),
               NavEntry("technical", f"1️⃣ Screener 1 · {name('technical', 'Technical')}", "technical", "screeners"),
               NavEntry("fundamental", f"2️⃣ Screener 2 · {name('fundamental', 'Fundamental')}", "fundamental", "screeners"),
               NavEntry("shariah", f"3️⃣ Screener 3 · {name('shariah', 'Shariah')}", "shariah", "screeners"),
               NavEntry("annual_report", "📑 Annual Report Intelligence", "annual_report"),
               NavEntry("data_management", "🗄️ Data Management"),
               NavEntry("execution_monitor", "⏱️ Execution Monitor"),
               NavEntry("custom_screeners", "🧩 Custom Screeners")]
        nav += [NavEntry(f"module:{s.id}", f"      ↳ {s.name}", s.id, "custom_screeners") for s in self.enabled("custom_screener")]
        nav.append(NavEntry("custom_analysis", "🔬 Custom Analysis"))
        nav += [NavEntry(f"module:{s.id}", f"      ↳ {s.name}", s.id, "custom_analysis") for s in self.enabled("custom_analysis")]
        nav.append(NavEntry("system_config", "⚙️ System Configuration"))
        return nav

    # ------------------------------------------------------------------ execution (isolated)
    def run(self, module_id: str, ctx, now: datetime | None = None) -> RunOutcome:
        """Run one module. NEVER raises (except KeyboardInterrupt/SystemExit): a failing module cannot affect the caller or any other module."""
        started = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
        s = self._specs.get(module_id)
        if s is None:
            return RunOutcome(module_id, "unknown", started, error="no such module" + (f" (invalid: {self.invalid[module_id]})" if module_id in self.invalid else ""))
        if not self.is_enabled(module_id):
            return RunOutcome(module_id, "disabled", started)
        t0 = time.monotonic()
        try:
            summary = resolve(s.run)(ctx) or {}
        except ConfigError as exc:
            return RunOutcome(module_id, "not_configured", started, time.monotonic() - t0, error=str(exc))
        except Exception as exc:
            return RunOutcome(module_id, "error", started, time.monotonic() - t0,
                              error=f"{type(exc).__name__}: {exc}\n" + "".join(traceback.format_exc().splitlines(True)[-6:]))
        return RunOutcome(module_id, "ok", started, time.monotonic() - t0, summary if isinstance(summary, dict) else {"result": str(summary)},
                          output_problems=self.check_output(s, ctx))

    @staticmethod
    def load_rows(spec: ModuleSpec, ctx):
        """Rows of the module's published results file per its output schema (None if missing/unreadable)."""
        f = spec.output_schema.get("results_file")
        if not f:
            return None
        try:
            data = json.loads((ctx.results_dir(spec.id) / f).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        key = spec.output_schema.get("rows_key")
        return data.get(key) if key and isinstance(data, dict) else data

    def check_output(self, spec: ModuleSpec, ctx) -> list[str]:
        if not spec.output_schema:
            return []
        rows = self.load_rows(spec, ctx)
        if rows is None:
            return [f"results file '{spec.output_schema['results_file']}' was not produced"]
        need = spec.output_schema.get("required_columns", [])
        if need and isinstance(rows, list):
            for i, r in enumerate(rows[:50]):
                miss = [c for c in need if not isinstance(r, dict) or c not in r]
                if miss:
                    return [f"row {i} is missing required column(s): {', '.join(miss)}"]
        return []
