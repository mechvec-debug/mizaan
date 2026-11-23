"""Screener 3 - Shariah Compliance Screener: the six criteria, strict (no assumptions), three-valued verdict."""
from .criteria import Outcome, evaluate
from .evidence import FIELDS as EVIDENCE_FIELDS, EvidenceStore
from .mapper import BseMapper, build_map, write_map
from .params import COMPLIANT, CRITERIA, NON_COMPLIANT, UNDETERMINED, ShariahParams
from .scanner import DISPLAY_NAME, MODULE_ID, ScanResult, evaluate_stock, run, scan_universe, write_outputs
from .xbrl import parse_xbrl_facts

__all__ = ["ShariahParams", "CRITERIA", "COMPLIANT", "NON_COMPLIANT", "UNDETERMINED", "evaluate", "Outcome", "EvidenceStore",
           "EVIDENCE_FIELDS", "BseMapper", "build_map", "write_map", "evaluate_stock", "scan_universe", "write_outputs", "run",
           "ScanResult", "parse_xbrl_facts", "MODULE_ID", "DISPLAY_NAME"]
