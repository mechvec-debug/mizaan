"""python -m mizan.annual_report run [--recover-now]   one daily batch (needs GEMINI_API_KEY and the limits in config/app.yaml)
   python -m mizan.annual_report status               print the dashboard numbers"""
import json
import sys
from datetime import datetime, timezone

from ..core.config import load_config
from ..core.errors import ConfigError
from ..core.paths import project_root, resolve
from ..data import DataManager
from ..data.providers.bse import BseXbrlProvider
from ..data.providers.yahoo import YahooProvider
from ..screeners.shariah.evidence import EvidenceStore
from ..screeners.shariah.mapper import BseMapper
from ..screeners.shariah.xbrl import parse_xbrl_facts
from ..universe import UniverseService
from .config import AnnualReportConfig
from .dashboard import summary
from .engine import run_batch
from .llm import GeminiClient
from .reports import BseReportProvider
from .state import QueueStore, UsageStore


def main(argv=None) -> int:
    a = list(sys.argv[1:] if argv is None else argv)
    cmd = a[0] if a else "status"
    cfg_all = load_config()
    cfg = AnnualReportConfig.from_config(cfg_all)
    store = resolve(cfg_all["paths"]["store"])
    if cmd == "status":
        base = store / "state" / "annual_report"
        print(json.dumps(summary(QueueStore(base / "queue.json"), UsageStore(base / "usage.json"), cfg, datetime.now(timezone.utc)), indent=1))
        return 0
    if cmd != "run":
        print(__doc__); return 2
    try:
        cfg.require_limits()
        llm = GeminiClient(model=cfg.model)
    except (ConfigError, ValueError) as exc:
        print("NOT RUNNING:", exc); return 2
    dm = DataManager(YahooProvider(), store, cfg_all["data"])
    provider = BseReportProvider(BseXbrlProvider(parse_xbrl_facts), parse_xbrl_facts)
    rep = run_batch(UniverseService().get().tickers, store_dir=store, provider=provider, llm=llm,
                    evidence=EvidenceStore(store / "state" / "shariah_evidence"), dm=dm,
                    mapper=BseMapper(project_root() / "data" / "universe" / "nse_bse_map.csv"), cfg=cfg, force_recover="--recover-now" in a)
    print({"date": rep.date, "stopped": rep.stopped, "processed": len(rep.processed), "failed": len(rep.failed), "discovered": rep.discovered,
           "tokens": rep.tokens_in + rep.tokens_out, "remaining": rep.remaining, "alerts": rep.alerts})
    return 0


if __name__ == "__main__":
    sys.exit(main())
