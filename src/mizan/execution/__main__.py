"""python -m mizan.execution run   [--modules a,b] [--retries N] [--time-budget-min M] [--trigger NAME] [--resume] [--retry-failed]
                                  [--publish] [--universe FILE] [--store DIR]
python -m mizan.execution status [--markdown]        python -m mizan.execution plan [--modules a,b]
Exit code: 0 ok | 1 a module failed (or publishing failed) | 2 another run holds the lock."""
import argparse
import json
import sys

from ..core.config import load_config
from ..core.paths import resolve
from ..dashboard.app import load_modules_yaml
from ..modules.builtin import build_registry
from ..modules.context import RunContext
from ..universe import UniverseService
from .manager import ExecutionManager


def _build(args):
    cfg = load_config()
    ctx = RunContext.live(cfg)
    if getattr(args, "universe", None):
        ctx.universe_service = UniverseService(resolve(args.universe))
    if getattr(args, "store", None):
        ctx.store = resolve(args.store)
    reg = build_registry(cfg, load_modules_yaml(ctx.root), state_path=ctx.store / "state" / "modules_state.json")
    return ctx, ExecutionManager(reg, ctx)


def _markdown(status: dict) -> str:
    if not status:
        return "No run recorded yet.\n"
    out = [f"### Run `{status['run_id']}` ({status['trigger']}) - exit code {status['exit_code']}",
           f"{status['started_at']} -> {status['finished_at']}" + (" - **time budget reached**" if status.get("deadline_hit") else ""), "",
           "| Module | Status | Seconds |", "|---|---|---|"]
    out += [f"| {m} | {v['status']} | {v['seconds']} |" for m, v in status["modules"].items()]
    if status["alerts"]:
        out += ["", "**Alerts**"] + [f"- {a['level'].upper()} {a['module']}: {a['message']}" for a in status["alerts"]]
    return "\n".join(out) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="mizan.execution")
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("run")
    r.add_argument("--modules", default="")
    r.add_argument("--retries", type=int)
    r.add_argument("--time-budget-min", type=float)
    r.add_argument("--trigger", default="manual")
    r.add_argument("--resume", action="store_true")
    r.add_argument("--retry-failed", action="store_true")
    r.add_argument("--publish", action="store_true")
    r.add_argument("--universe")
    r.add_argument("--store")
    s = sub.add_parser("status")
    s.add_argument("--markdown", action="store_true")
    s.add_argument("--store")
    p = sub.add_parser("plan")
    p.add_argument("--modules", default="")
    a = ap.parse_args(argv)
    if a.cmd not in ("run", "status", "plan"):
        print(__doc__)
        return 2
    ctx, mgr = _build(a)
    mods = [m for m in getattr(a, "modules", "").split(",") if m]
    if a.cmd == "plan":
        print("would run, in order:", mgr.select(mods or None))
        return 0
    if a.cmd == "status":
        st = json.loads(mgr.status_path.read_text(encoding="utf-8")) if mgr.status_path.exists() else {}
        print(_markdown(st) if a.markdown else json.dumps({"status": st, "jobs": mgr.jobs.latest_jobs()}, indent=1))
        return 0
    snap = ctx.universe()
    print(f"universe: {ctx.universe_service.path.name} ({snap.count} stocks) | store: {ctx.store}")
    rep = mgr.run(mods or None, retries=a.retries, time_budget_minutes=a.time_budget_min, trigger=a.trigger, resume=a.resume, retry_failed=a.retry_failed)
    for m, v in rep.modules.items():
        print(f"{m:<14} {v['status']:<15} {v['seconds']:>8}s")
    for al in rep.alerts:
        print(f"  [{al['level'].upper()}] {al['module']}: {al['message']}")
    code = rep.exit_code
    if a.publish and not rep.locked:
        from ..publish import PublishError, publish_all
        try:
            man = publish_all(ctx.store, snap)
            print(f"published {len(man['files'])} files (missing modules: {man['missing'] or 'none'})")
        except PublishError as exc:
            print("PUBLISH FAILED:", exc)
            code = 1
    return code


if __name__ == "__main__":
    sys.exit(main())
