"""Record or inspect evidence by hand (admin tool; the annual-report analysis writes the same store).

  python -m mizan.screeners.shariah.evidence_cli set TCS haram_business_revenue 0 --fy 2026-03-31 --evidence "Segment note 38: single IT-services segment"
  python -m mizan.screeners.shariah.evidence_cli show TCS
Fields: haram_business_revenue | impermissible_interest_income | non_compliant_investments   (Rs crore; 0 only if the filing states nil)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ...core.config import load_config
from ...core.paths import resolve
from ...universe.normalize import invalid_reason, normalize_symbol
from .evidence import FIELDS, SOURCES, EvidenceStore


def main(argv=None, store_dir: str | Path | None = None) -> int:
    ap = argparse.ArgumentParser(prog="evidence_cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("set")
    s.add_argument("ticker"); s.add_argument("field", choices=FIELDS); s.add_argument("value_cr", type=float)
    s.add_argument("--fy", required=True, help="fiscal year end of the statements, YYYY-MM-DD")
    s.add_argument("--evidence", required=True, help="where in the filing this figure comes from")
    s.add_argument("--source", choices=SOURCES, default="manual")
    w = sub.add_parser("show")
    w.add_argument("ticker")
    a = ap.parse_args(argv)
    sym = normalize_symbol(a.ticker)
    if invalid_reason(sym):
        print(f"invalid ticker {a.ticker!r}: {invalid_reason(sym)}", file=sys.stderr)
        return 2
    store = EvidenceStore(store_dir or resolve(load_config()["paths"]["store"]) / "state" / "shariah_evidence")
    if a.cmd == "show":
        print(json.dumps(store.get(sym), indent=1, ensure_ascii=False))
        return 0
    try:
        store.set(sym, a.field, a.value_cr, source=a.source, fy_end=a.fy, evidence=a.evidence)
    except ValueError as exc:
        print(f"not saved: {exc}", file=sys.stderr)
        return 2
    print(f"saved {sym} {a.field} = {a.value_cr} Cr (FY {a.fy}, {a.source})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
