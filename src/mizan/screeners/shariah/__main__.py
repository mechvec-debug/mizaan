"""python -m mizan.screeners.shariah  - run the screen on the current universe with live data."""
import sys

from ...core.config import load_config
from ...core.paths import project_root, resolve
from ...data import DataManager
from ...data.providers.bse import BseXbrlProvider
from ...data.providers.yahoo import YahooProvider
from ...universe import UniverseService
from .evidence import EvidenceStore
from .mapper import BseMapper
from .params import ShariahParams
from .scanner import run
from .xbrl import parse_xbrl_facts


def main() -> int:
    cfg = load_config()
    store = resolve(cfg["paths"]["store"])
    dm = DataManager(YahooProvider(cfg["data"]["throttle"].get("timeout_seconds", 30)), store, cfg["data"])
    mapper = BseMapper(project_root() / "data" / "universe" / "nse_bse_map.csv")
    meta = run(UniverseService().get(), dm, store / "results" / "shariah", mapper, EvidenceStore(store / "state" / "shariah_evidence"),
               ShariahParams(), xbrl_provider=BseXbrlProvider(parse_xbrl_facts))
    print({k: meta[k] for k in ("status", "requested", "evaluated", "carried_forward", "failed", "by_verdict", "by_source")})
    return 1 if meta["status"] == "no_data" else 0


if __name__ == "__main__":
    sys.exit(main())
