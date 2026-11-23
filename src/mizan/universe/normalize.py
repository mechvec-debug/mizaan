"""Ticker normalisation + validation. ONE definition used by every module.

Rules (documented in docs/UNIVERSE.md):
  * Unicode NFKC, trim, remove wrapping quotes, UPPER CASE
  * Remove a leading exchange prefix  NSE: / BSE:
  * Remove a trailing exchange suffix .NS / .BO   (the data layer adds provider suffixes itself)
  * A valid symbol matches  [A-Z0-9][A-Z0-9&-]{0,19}   (so M&M, BAJAJ-AUTO, ARE&M are valid)
The ORIGINAL text of every entry is always kept next to the normalised value (identifier preservation).
"""
from __future__ import annotations

import re
import unicodedata

SYMBOL_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9&\-]{0,19}$")
_PREFIXES = ("NSE:", "BSE:")
_SUFFIXES = (".NS", ".BO")


def normalize_symbol(raw: str) -> str:
    s = unicodedata.normalize("NFKC", str(raw)).strip().strip("'\"").strip().upper()
    for p in _PREFIXES:
        if s.startswith(p):
            s = s[len(p):].strip()
    for suf in _SUFFIXES:
        if s.endswith(suf):
            s = s[: -len(suf)].strip()
    return s


def invalid_reason(symbol: str) -> str | None:
    """None if valid, else a human-readable reason."""
    if not symbol:
        return "empty symbol"
    if SYMBOL_PATTERN.match(symbol):
        return None
    if " " in symbol or "\t" in symbol:
        return "contains whitespace (one symbol per line)"
    if len(symbol) > 20:
        return "longer than 20 characters"
    if not re.match(r"^[A-Z0-9]", symbol):
        return "must start with a letter or digit"
    bad = sorted({c for c in symbol if not re.match(r"[A-Z0-9&\-]", c)})
    return "contains invalid character(s): " + " ".join(repr(c) for c in bad)


def to_yahoo(symbol: str) -> str:
    """Yahoo form. Identical to the original Fundamental screener behaviour: append .NS."""
    return f"{symbol}.NS"
