"""Run the modules once on the TEST universe (6 stocks) and print what happened.

    python scripts/run_test_batch.py                    # technical + fundamental + shariah, live data
    python scripts/run_test_batch.py --annual           # also today's annual-report batch (needs the two limits + GEMINI_API_KEY)
    python scripts/run_test_batch.py --universe data/universe/stock_universe.txt --store data_store     # the real thing

Defaults: data/universe/test_universe.txt and data_store_test (so test data never mixes with the real store).
Then:  streamlit run src/mizan/dashboard/app.py   with config/app.local.yaml pointing at the same two paths."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mizan.core.config import load_config  # noqa: E402
from mizan.core.paths import resolve  # noqa: E402
from mizan.dashboard.app import build_app  # noqa: E402
from mizan.execution.runner import run_modules  # noqa: E402
from mizan.modules.context import RunContext  # noqa: E402
from mizan.universe import UniverseService  # noqa: E402


def print_reasons(ctx, ids) -> None:
    """The one-line summaries hide WHY a stock failed or was skipped; show the reasons from each run report."""
    import json
    for mid in ids:
        f = ctx.results_dir(mid) / "run_report.json"
        if not f.exists():
            continue
        r = json.loads(f.read_text(encoding="utf-8"))
        for title, key in (("failed", "failed_detail"), ("skipped", "skipped_detail"), ("degraded", "degraded_detail"), ("notes", "data_notes")):
            d = r.get(key) or {}
            if d:
                print(f"\n[{mid}] {title}:")
                for k, v in list(d.items())[:12]:
                    print(f"    {k}: {v}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", default="data/universe/test_universe.txt")
    ap.add_argument("--store", default="data_store_test")
    ap.add_argument("--annual", action="store_true")
    a = ap.parse_args()
    cfg = load_config()
    ctx = RunContext.live(cfg)
    ctx.universe_service, ctx.store = UniverseService(resolve(a.universe)), resolve(a.store)
    ctx, _ = build_app(cfg, ctx=ctx)
    snap = ctx.universe()
    print(f"universe: {ctx.universe_service.path.name} -> {snap.count} stocks: {', '.join(snap.tickers[:10])}{' ...' if snap.count > 10 else ''}\nstore: {ctx.store}\n")
    ids = ["technical", "fundamental", "shariah"] + (["annual_report"] if a.annual else [])
    bad = 0
    for o in run_modules(ctx.registry, ctx, ids):
        print(f"{o.module_id:<14} {o.status:<15} {o.seconds:6.1f}s  {({k: v for k, v in o.summary.items() if not isinstance(v, (list, dict))} or '')}")
        if o.error:
            print("   ", o.error.splitlines()[0])
        for p in o.output_problems:
            print("    warning:", p)
        bad += o.status == "error"
    print_reasons(ctx, ids)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
