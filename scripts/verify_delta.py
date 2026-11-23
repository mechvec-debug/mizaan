"""Verify a delta package against the project (standard library only).

  python scripts/verify_delta.py                       # after extracting the delta over the project: every file must match
  python scripts/verify_delta.py --check-base --manifest <extracted>/docs/delta/phase8_manifest.json --project <your project>
        # BEFORE copying: lists files you changed locally that the delta would overwrite
"""
import argparse
import hashlib
import json
import re
import sys
from pathlib import Path


def manifest_key(path) -> tuple:
    """Order delta manifests by phase NUMBER (phase7 < phase9 < phase9b < phase9c < phase11), not alphabetically."""
    m = re.match(r"phase(\d+)(.*)_manifest\.json$", Path(path).name)
    return (int(m.group(1)), m.group(2)) if m else (10**6, str(path))


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default=".")
    ap.add_argument("--manifest", default=None)
    ap.add_argument("--check-base", action="store_true")
    a = ap.parse_args()
    root = Path(a.project).resolve()
    found = sorted((root / "docs" / "delta").glob("phase*_manifest.json"), key=manifest_key)
    man = Path(a.manifest) if a.manifest else (found[-1] if found else root / "docs" / "delta" / "manifest.json")
    m = json.loads(man.read_text(encoding="utf-8"))
    if a.check_base:
        changed = [p for p, v in m["modified"].items() if (root / p).exists() and sha(root / p) != v["base_sha256"]]
        for p in changed:
            print("LOCALLY CHANGED (would be overwritten):", p)
        print("base check:", "OK - your files match the package this delta applies on top of" if not changed else f"{len(changed)} file(s) differ from the package this delta applies on top of")
        return 1 if changed else 0
    bad = 0
    for p, want in {**m["added"], **{k: v["sha256"] for k, v in m["modified"].items()}}.items():
        f = root / p
        state = "MISSING" if not f.exists() else ("OK" if sha(f) == want else "DIFFERENT")
        bad += state != "OK"
        if state != "OK":
            print(f"{state:<9} {p}")
    n = len(m["added"]) + len(m["modified"])
    print(f"checked {n} files: {'ALL OK' if not bad else str(bad) + ' problem(s)'}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
