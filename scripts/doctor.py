"""Checks your environment and project files and says what to fix.   python scripts/doctor.py     (standard library only)

1 which Python / virtual environment is running      2 which packages and versions are installed
3 whether the `streamlit` command belongs to THIS environment, and whether Streamlit imports
4 key settings in config/app.yaml                     5 which delta files are missing (all phases) or differ (latest phase)
6 the reasons recorded by the last runs (failed / skipped stocks) in every data folder"""
import hashlib
import importlib.metadata as md
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OK, WARN, BAD = "  [ok]  ", "  [!]   ", "  [FIX] "


def manifest_key(path) -> tuple:
    """Order delta manifests by phase NUMBER (phase7 < phase9 < phase9b < phase9c < phase11), not alphabetically."""
    m = re.match(r"phase(\d+)(.*)_manifest\.json$", Path(path).name)
    return (int(m.group(1)), m.group(2)) if m else (10**6, str(path))


def say(tag, msg):
    print(tag + msg)


def ver(pkg):
    try:
        return md.version(pkg)
    except md.PackageNotFoundError:
        return None


def main() -> int:
    problems = 0
    print(f"\n== 1. Python\n   {sys.version.split()[0]}  {sys.executable}")
    venv = sys.prefix != sys.base_prefix
    say(OK if venv else WARN, "running inside a virtual environment" if venv else "NOT inside a virtual environment")

    print("\n== 2. Packages")
    for pkg, need in (("pandas", 1), ("numpy", 1), ("yfinance", 1), ("lxml", 1), ("requests", 1), ("pyyaml", 1), ("pyarrow", 0),
                      ("streamlit", 1), ("starlette", 0), ("pypdf", 0), ("google-genai", 0)):
        v = ver(pkg)
        if v:
            say(OK, f"{pkg} {v}")
        else:
            say(BAD if need else WARN, f"{pkg} is not installed" + ("  ->  python -m pip install -r requirements-dev.txt" if need else " (needed later / optional)"))
            problems += bool(need)

    print("\n== 3. Streamlit")
    exe = shutil.which("streamlit")
    mine = Path(sys.prefix) / ("Scripts" if sys.platform == "win32" else "bin")
    if exe:
        same = Path(exe).resolve().parent == mine.resolve()
        say(OK if same else BAD, f"`streamlit` command -> {exe}" + ("" if same else "\n            That is NOT this environment's streamlit. Start the dashboard with:  python -m streamlit run src/mizan/dashboard/app.py"))
        problems += not same
    else:
        say(WARN, "no `streamlit` command on PATH (use:  python -m streamlit run src/mizan/dashboard/app.py)")
    r = subprocess.run([sys.executable, "-c", "import streamlit"], capture_output=True, text=True)
    if r.returncode == 0:
        say(OK, "this environment can import streamlit")
    else:
        last = (r.stderr.strip().splitlines() or ["?"])[-1]
        say(BAD, f"importing streamlit fails here: {last}\n            Fix:  python -m pip install -U streamlit starlette     then run:  python -m pip check")
        problems += 1

    print("\n== 4. config/app.yaml")
    try:
        import yaml
        cfg = yaml.safe_load((ROOT / "config" / "app.yaml").read_text(encoding="utf-8"))
        d, t = cfg.get("data", {}), cfg.get("data", {}).get("throttle", {})
        say(OK if "xbrl" in d.get("cache_freshness_hours", {}) else BAD, "cache_freshness_hours has the xbrl entries")
        say(OK if "weights" in t else BAD, "throttle.weights (real-request accounting) present")
        say(OK if "annual_report" in cfg and "model" in cfg["annual_report"] else BAD, "annual_report section complete")
        lim = cfg.get("annual_report", {})
        say(WARN if not lim.get("daily_token_budget") else OK, f"annual-report limits: budget={lim.get('daily_token_budget')} rpm={lim.get('requests_per_minute')} (open is fine for now)")
        local = ROOT / "config" / "app.local.yaml"
        say(OK, f"test mode ON: {local.read_text(encoding='utf-8').strip().splitlines()[-2:]}" if local.exists() else "test mode off (no config/app.local.yaml)")
    except Exception as exc:
        say(BAD, f"cannot read config/app.yaml: {type(exc).__name__}: {exc}")
        problems += 1
    import os
    envfile = ROOT / ".env"
    env_hash = os.environ.get("MIZAN_ADMIN_PASSWORD_HASH") or any(
        l.strip().startswith("MIZAN_ADMIN_PASSWORD_HASH=") and len(l.strip()) > 30 for l in (envfile.read_text(encoding="utf-8").splitlines() if envfile.exists() else []))
    say(OK if env_hash else WARN, "admin login is set up (password hash found)" if env_hash else
        "admin login is NOT set up - run:  python -m mizan.core.auth hash --write-env   then restart the dashboard")
    print("   code version check:")
    sys.path.insert(0, str(ROOT / "src"))
    try:
        from mizan.data.keys import DATA_VERSIONS
        from mizan.data.manager import DataManager
        say(OK if "xbrl" in DATA_VERSIONS else BAD, f"data/keys.py knows 'xbrl' ({sorted(DATA_VERSIONS)})")
        say(OK if hasattr(DataManager, "get_xbrl_facts") and hasattr(DataManager, "_limiter") else BAD, "data/manager.py has get_xbrl_facts and per-source limiters")
        problems += ("xbrl" not in DATA_VERSIONS) or not (hasattr(DataManager, "get_xbrl_facts") and hasattr(DataManager, "_limiter"))
    except Exception as exc:
        say(BAD, f"cannot import the project code: {type(exc).__name__}: {exc}")
        problems += 1

    print("\n== 5. Delta files (every file is compared with the newest manifest that lists it)")
    mans = sorted((ROOT / "docs" / "delta").glob("phase*_manifest.json"), key=manifest_key)
    if not mans:
        say(BAD, "no docs/delta/phase*_manifest.json found - no delta has been applied?")
    expect, owner = {}, {}
    for m in mans:
        man = json.loads(m.read_text(encoding="utf-8"))
        for p, h in {**man["added"], **{k: v["sha256"] for k, v in man["modified"].items()}}.items():
            expect[p], owner[p] = h, m.name
    missing = [p for p in expect if not (ROOT / p).exists()]
    stale = [p for p, h in expect.items() if (ROOT / p).exists() and hashlib.sha256((ROOT / p).read_bytes()).hexdigest() != h]
    say(OK if not missing and not stale else BAD, f"{len(mans)} manifests, {len(expect)} files checked: missing {len(missing)}, not matching {len(stale)}")
    for p in (missing + stale)[:15]:
        print(f"            {'MISSING  ' if p in missing else 'DIFFERENT'} {p}   (from {owner[p]})")
    if stale:
        print("            DIFFERENT = edited after the delta, or an older copy was left in place. Re-extract that delta over the project.")
    problems += bool(missing or stale)
    tests = sorted(p.name for p in (ROOT / "tests" / "unit").glob("test_*.py"))
    print(f"   test files in tests/unit: {len(tests)}")

    print("\n== 6. Reasons recorded by the last runs")
    found = False
    for store in sorted(ROOT.glob("data_store*")):
        for rep in sorted(store.glob("results/*/run_report.json")):
            r = json.loads(rep.read_text(encoding="utf-8"))
            print(f"   -- {store.name}/{rep.parent.name} report written {r.get('run_at', 'at an unknown time')} (reasons below are from THAT run)")
            for title, key in (("failed", "failed_detail"), ("skipped", "skipped_detail"), ("notes", "data_notes")):
                for k, v in list((r.get(key) or {}).items())[:12]:
                    found = True
                    print(f"   {store.name}/{rep.parent.name} {title}: {k}: {v}")
    if not found:
        print("   (none recorded)")
    print(f"\nRESULT: {'everything looks fine' if not problems else str(problems) + ' thing(s) to fix - see [FIX] lines above'}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
