"""Pure text -> entries parsing (no file or global state: trivially testable)."""
from __future__ import annotations

import re

from .models import Entry
from .normalize import invalid_reason, normalize_symbol

_SINGLE_TOKEN = re.compile(r"^\S+$")


def parse_text(text: str) -> list[Entry]:
    """File format: one symbol per line; '#' starts a comment (a trailing '# note' is kept as a note);
    a comment whose body is a single token (e.g. '# TCS') is a DISABLED stock. Blank lines ignored."""
    entries: list[Entry] = []
    seen: dict[str, int] = {}
    for i, line in enumerate(text.lstrip("\ufeff").splitlines(), start=1):
        raw = line.strip()
        if not raw:
            continue
        if raw.startswith("#"):
            body = raw.lstrip("#").strip()
            if body and _SINGLE_TOKEN.match(body):
                sym = normalize_symbol(body)
                if invalid_reason(sym) is None:
                    entries.append(Entry(i, raw, sym, "disabled"))
            continue                                    # ordinary comment
        code, _, note = raw.partition("#")
        code, note = code.strip(), note.strip() or None
        sym = normalize_symbol(code)
        reason = invalid_reason(sym)
        if reason:
            entries.append(Entry(i, raw, sym, "invalid", reason, note))
        elif sym in seen:
            entries.append(Entry(i, raw, sym, "duplicate", f"already listed on line {seen[sym]}", note))
        else:
            seen[sym] = i
            entries.append(Entry(i, raw, sym, "active", None, note))
    return entries


_DELIM = re.compile(r"[,;\t]")
_HEADER_KEYS = ("symbol", "ticker", "code", "nse symbol", "nse_symbol")


def parse_bulk(text: str) -> list[str]:
    """Bulk-upload text -> list of RAW symbol strings (validation happens when they are added).

    * plain list      : one symbol per line (a line with spaces is kept whole and later rejected as invalid)
    * CSV / TSV       : needs a header row with a column named symbol / ticker / code - that column is used
    * single-line list: 'TCS, INFY, WIPRO' (one line with delimiters) is accepted
    Anything ambiguous raises ValueError - we never guess which column holds the tickers."""
    lines = [l.strip() for l in text.lstrip("\ufeff").splitlines() if l.strip() and not l.strip().startswith("#")]
    if not lines:
        return []
    if not any(_DELIM.search(l) for l in lines):
        return lines
    header = [h.strip().strip("\"'").lower() for h in _DELIM.split(lines[0])]
    col = next((header.index(k) for k in _HEADER_KEYS if k in header), None)
    if col is not None:
        out = []
        for l in lines[1:]:
            cells = _DELIM.split(l)
            if len(cells) > col and cells[col].strip():
                out.append(cells[col].strip())
        return out
    if len(lines) == 1:
        return [c.strip() for c in _DELIM.split(lines[0]) if c.strip()]
    raise ValueError("Delimited file needs a header row with a column named 'symbol', 'ticker' or 'code' "
                     f"(found header: {header}). Refusing to guess which column holds the tickers.")
