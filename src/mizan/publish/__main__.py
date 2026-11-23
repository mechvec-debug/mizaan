"""python -m mizan.publish build | validate   [--store PATH]"""
import sys
from pathlib import Path

from ..core.config import load_config
from ..core.paths import resolve
from ..universe import UniverseService
from .contract import PublishError, publish_all, validate_published


def main(argv=None) -> int:
    a = list(sys.argv[1:] if argv is None else argv)
    cmd = a[0] if a else ""
    store = Path(a[a.index("--store") + 1]) if "--store" in a else resolve(load_config()["paths"]["store"])
    if cmd == "build":
        try:
            m = publish_all(store, UniverseService().get())
        except PublishError as exc:
            print("PUBLISH FAILED:", exc)
            return 1
        print(f"published {len(m['files'])} files to {store / 'published' / 'v1'} (missing modules: {m['missing'] or 'none'})")
        return 0
    if cmd == "validate":
        probs = validate_published(store / "published" / "v1")
        print("OK - consistent" if not probs else "\n".join(probs))
        return 1 if probs else 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
