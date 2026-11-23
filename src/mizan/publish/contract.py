"""Published data contract v1 - ONE set of static JSON files that every front end reads (Streamlit, Cloudflare Pages, the Android app).
Written to <store>/published/v1/. manifest.json lists every file with SHA-256, size, row count and 'as of' date and is written LAST, so a
reader that checks the hashes never sees a half-updated set. Files are deterministic (no wall-clock time inside, one row per line), so
unchanged results make no git change. A schema change bumps the folder (v2/); v1 never changes shape."""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from ..core.errors import MizanError
from ..core.io import atomic_write_text

SCHEMA = 1
HEADERS = "/*\n  Access-Control-Allow-Origin: *\n  Cache-Control: public, max-age=300\n  X-Content-Type-Options: nosniff\n"
INDEX = ("<!doctype html><meta charset=utf-8><title>Mizan published data</title><h1>Mizan published data</h1>"
         "<p>Start at <a href=v1/manifest.json>v1/manifest.json</a> (files, hashes, freshness).</p>\n")
NAMES = {"technical": "Swing Setup Radar", "fundamental": "Quality Value Rebound", "shariah": "Shariah Compliance Screener",
         "annual_report": "Annual Report Intelligence"}


class PublishError(MizanError):
    pass


def _read(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _dumps(obj: dict) -> str:
    dump = lambda o: json.dumps(o, ensure_ascii=False, separators=(",", ":"))
    rows = obj.get("rows")
    if not isinstance(rows, list):
        return dump(obj)
    return dump({k: v for k, v in obj.items() if k != "rows"})[:-1] + ',"rows":[\n' + ",\n".join(dump(r) for r in rows) + "\n]}"


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def build_published(store, universe=None, now: datetime | None = None) -> dict:
    """Write published/v1/* from the module results. A module with no results is listed under 'missing' (never invented); an older
    published file of a module whose results vanished is kept and flagged stale."""
    store = Path(store)
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    out, res, files, missing = store / "published" / "v1", store / "results", {}, []

    def put(name, obj, rows, as_of):
        data = _dumps(obj).encode("utf-8")
        atomic_write_text(out / name, data.decode("utf-8"))
        files[name] = {"path": f"v1/{name}", "sha256": _sha(data), "bytes": len(data), "rows": rows, "as_of": as_of}

    def keep_stale(name):
        old = out / name
        if old.exists():
            b = old.read_bytes()
            j = json.loads(b)
            files[name] = {"path": f"v1/{name}", "sha256": _sha(b), "bytes": len(b), "rows": j.get("count"), "as_of": j.get("as_of"), "stale": True}
        else:
            missing.append(name.removesuffix(".json"))

    head = lambda mod: {"schema": SCHEMA, "module": mod, "display_name": NAMES.get(mod, mod)}
    tech = _read(res / "technical" / "trading_alerts.json")
    if tech is not None:
        asof = max((str(r.get("time_received", ""))[:10] for r in tech), default=None)
        put("technical.json", {**head("technical"), "as_of": asof, "count": len(tech), "rows": tech}, len(tech), asof)
    else:
        keep_stale("technical.json")
    fund = _read(res / "fundamental" / "data.json")
    if fund is not None:
        asof = fund[0].get("Updated") if fund else None
        put("fundamental.json", {**head("fundamental"), "as_of": asof, "count": len(fund), "rows": fund}, len(fund), asof)
    else:
        keep_stale("fundamental.json")
    sh = _read(res / "shariah" / "latest_results.json")
    if sh is not None:
        rows = sh.get("data", [])
        vc = Counter(r.get("verdict") or ("COMPLIANT" if r.get("is_compliant") else "NON_COMPLIANT") for r in rows)
        kp = {"total": len(rows), "compliant": vc.get("COMPLIANT", 0), "non_compliant": vc.get("NON_COMPLIANT", 0), "undetermined": vc.get("UNDETERMINED", 0)}
        put("shariah.json", {**head("shariah"), "as_of": sh.get("last_updated"), "count": len(rows), "kpis": kp, "rows": rows}, len(rows), sh.get("last_updated"))
    else:
        keep_stale("shariah.json")
    ann = _read(res / "annual_report" / "dashboard.json")
    if ann is not None:
        put("annual_report.json", {**head("annual_report"), "as_of": ann.get("as_of"), "count": ann.get("reports_processed"), "summary": ann}, ann.get("reports_processed"), ann.get("as_of"))
    else:
        keep_stale("annual_report.json")
    if universe is not None:
        put("universe.json", {**head("universe"), "as_of": stamp[:10], "count": universe.count, "version": universe.version, "tickers": list(universe.tickers)}, universe.count, stamp[:10])
    status = _read(store / "state" / "execution" / "status.json") or {}
    mods = {m: {"status": (status.get("modules", {}).get(m) or {}).get("status"), "as_of": files.get(f"{m}.json", {}).get("as_of"),
                "published": f"{m}.json" in files, "stale": bool(files.get(f"{m}.json", {}).get("stale"))} for m in NAMES}
    last = {k: status.get(k) for k in ("run_id", "trigger", "started_at", "finished_at", "exit_code", "deadline_hit", "alerts")}
    last["modules"] = {m: v.get("status") for m, v in status.get("modules", {}).items()}
    put("status.json", {**head("status"), "generated_at": stamp, "as_of": stamp, "last_run": last, "modules": mods}, None, stamp)
    manifest = {"schema": SCHEMA, "generated_at": stamp, "universe_version": getattr(universe, "version", None),
                "universe_count": getattr(universe, "count", None), "files": files, "modules": mods, "missing": missing}
    atomic_write_text(out / "manifest.json", json.dumps(manifest, indent=1, ensure_ascii=False) + "\n")     # LAST
    atomic_write_text(store / "published" / "_headers", HEADERS)
    atomic_write_text(store / "published" / "index.html", INDEX)
    return manifest


def validate_published(out_dir) -> list[str]:
    """Problems in a published/v1 folder (empty list = consistent and safe to serve)."""
    out, problems = Path(out_dir), []
    man = _read(out / "manifest.json")
    if not isinstance(man, dict):
        return ["manifest.json is missing or is not valid JSON"]
    if man.get("schema") != SCHEMA:
        problems.append(f"manifest schema is {man.get('schema')!r}, expected {SCHEMA}")
    for name, meta in (man.get("files") or {}).items():
        f = out / name
        if not f.exists():
            problems.append(f"{name}: listed in the manifest but missing")
            continue
        b = f.read_bytes()
        if _sha(b) != meta.get("sha256"):
            problems.append(f"{name}: content does not match the manifest checksum")
            continue
        try:
            j = json.loads(b)
        except ValueError:
            problems.append(f"{name}: not valid JSON")
            continue
        if j.get("schema") != SCHEMA:
            problems.append(f"{name}: schema {j.get('schema')!r}, expected {SCHEMA}")
        if meta.get("rows") is not None and isinstance(j.get("rows"), list) and len(j["rows"]) != meta["rows"]:
            problems.append(f"{name}: row count differs from the manifest")
    return problems


def publish_all(store, universe=None, now=None) -> dict:
    """Build, then validate; an inconsistent set is an error (the caller must not deploy it)."""
    man = build_published(store, universe, now)
    problems = validate_published(Path(store) / "published" / "v1")
    if problems:
        raise PublishError("published set is inconsistent: " + "; ".join(problems))
    return man
