"""python -m mizan.screeners.fundamental  - run the scan on the current universe with live Yahoo data."""
import sys

from ...core.config import load_config
from ...core.paths import resolve
from ...data import DataManager
from ...data.providers.yahoo import YahooProvider
from ...universe import UniverseService
from .params import FundamentalParams
from .scanner import run


def main() -> int:
    cfg = load_config()
    store = resolve(cfg["paths"]["store"])
    dm = DataManager(YahooProvider(cfg["data"]["throttle"].get("timeout_seconds", 30)), store, cfg["data"])
    meta = run(UniverseService().get(), dm, store / "results" / "fundamental", FundamentalParams())
    print({k: meta[k] for k in ("status", "requested", "evaluated", "carried_forward", "skipped", "failed", "by_status")})
    return 1 if meta["status"] == "no_data" else 0


if __name__ == "__main__":
    sys.exit(main())
