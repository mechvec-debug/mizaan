"""Command line:  python -m mizan.universe  <command>
   check                  validate the file and print a summary
   list                   print active tickers
   add SYM [SYM ...]      add symbols          remove / disable / enable SYM [SYM ...]
   import FILE            bulk add from a .txt / .csv file
"""
import sys

from .service import UniverseService


def _print_report(rep):
    for k in ("added", "removed", "disabled", "enabled", "already_present", "exists_but_disabled", "not_found"):
        v = getattr(rep, k)
        if v:
            print(f"{k:>20}: {len(v)}  {', '.join(v[:15])}{' ...' if len(v) > 15 else ''}")
    for raw, why in rep.invalid:
        print(f"{'invalid':>20}: {raw!r} - {why}")
    if rep.backup:
        print(f"{'backup':>20}: {rep.backup}")
    if rep.new_version:
        print(f"{'new version':>20}: {rep.new_version}")


def main(argv=None) -> int:
    a = list(sys.argv[1:] if argv is None else argv)
    cmd, rest = (a[0] if a else "check"), a[1:]
    svc = UniverseService()
    if cmd == "check":
        s = svc.get()
        print(s.summary())
        for e in s.invalid:
            print(f"  INVALID line {e.line_no}: {e.raw!r} - {e.reason}")
        for e in s.duplicates:
            print(f"  DUPLICATE line {e.line_no}: {e.raw!r} - {e.reason}")
        return 0 if s.ok else 1
    if cmd == "list":
        print("\n".join(svc.get().tickers)); return 0
    if cmd in ("add", "remove", "disable", "enable") and rest:
        _print_report(getattr(svc, cmd)(rest)); return 0
    if cmd == "import" and rest:
        with open(rest[0], encoding="utf-8-sig") as fh:
            _print_report(svc.add_from_text(fh.read()))
        return 0
    print(__doc__); return 2


if __name__ == "__main__":
    raise SystemExit(main())
