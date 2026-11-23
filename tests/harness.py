"""
Loads the ORIGINAL (unmodified) screener code for characterization / parity tests.

Network libraries are replaced by stubs so the original logic can run offline on
synthetic or frozen data.  Nothing in /original is edited.
"""
import importlib.util
import os
import sys
import types

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ORIG = os.path.join(ROOT, "original")


class FakeTicker:
    """Stand-in for yfinance.Ticker. Set FakeTicker.registry[symbol] = FakeTicker(...)."""
    registry = {}

    def __init__(self, symbol="", info=None, hist=None, income=None, balance=None, financials=None, mcap=None):
        if getattr(self, "_inited", False):      # registry hit: __new__ returned a configured object
            return
        self._inited = True
        self._symbol = symbol
        self._info = info or {}
        self._hist = hist
        self.income_stmt = income
        self.balance_sheet = balance
        self.financials = financials
        self.fast_info = types.SimpleNamespace(market_cap=mcap)

    def __new__(cls, symbol="", *a, **k):
        if symbol in cls.registry and not a and not k:
            return cls.registry[symbol]
        return super().__new__(cls)

    @property
    def info(self):
        return self._info

    def history(self, period=None, **k):
        return self._hist


def install_stubs():
    yf = types.ModuleType("yfinance")
    yf.Ticker = FakeTicker
    sys.modules["yfinance"] = yf
    gs = types.ModuleType("gspread")
    gs.authorize = lambda *a, **k: None
    sys.modules["gspread"] = gs
    g = types.ModuleType("google")
    go = types.ModuleType("google.oauth2")
    sa = types.ModuleType("google.oauth2.service_account")

    class Credentials:
        @staticmethod
        def from_service_account_info(*a, **k): return None
        @staticmethod
        def from_service_account_file(*a, **k): return None
    sa.Credentials = Credentials
    sys.modules.update({"google": g, "google.oauth2": go, "google.oauth2.service_account": sa})


def load_fundamental():
    install_stubs()
    spec = importlib.util.spec_from_file_location("orig_fundamental", os.path.join(ORIG, "fundamental", "dashboard_worker.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def load_shariah():
    """The original code imports `screener.*`; expose the flat folder under that name."""
    install_stubs()
    pkg = types.ModuleType("screener")
    pkg.__path__ = [os.path.join(ORIG, "shariah")]
    sys.modules["screener"] = pkg
    import screener.constants, screener.engine, screener.xbrl_parser  # noqa
    return sys.modules["screener.constants"], sys.modules["screener.engine"], sys.modules["screener.xbrl_parser"]
