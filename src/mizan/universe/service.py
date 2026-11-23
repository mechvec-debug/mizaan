"""Stock Universe Service - the ONLY code allowed to read or write the universe file.

Every module asks `UniverseService.get()` for an immutable snapshot. The service re-reads the file whenever it
changes on disk (size / mtime), so an edited list is picked up automatically by all modules on their next run.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

from ..core.config import load_config
from ..core.errors import EmptyUniverseError, UniverseError, UniverseValidationError
from ..core.paths import resolve
from .models import ChangeReport, Entry, UniverseSnapshot
from .normalize import invalid_reason, normalize_symbol
from .parser import parse_bulk, parse_text

log = logging.getLogger("mizan.universe")
_BACKUPS_KEPT = 20


class UniverseService:
    def __init__(self, path: str | os.PathLike | None = None, strict: bool = False):
        if path is None:
            cfg = load_config()
            path = resolve(cfg["paths"]["universe"])
        self.path = Path(path)
        self.strict = strict
        self._lock = threading.RLock()
        self._sig: tuple[int, int] | None = None
        self._snap: UniverseSnapshot | None = None

    # ------------------------------------------------------------------ read
    def _signature(self) -> tuple[int, int]:
        st = self.path.stat()
        return (st.st_mtime_ns, st.st_size)

    def get(self, strict: bool | None = None) -> UniverseSnapshot:
        """Current snapshot (reloaded automatically if the file changed).

        Never silent: an empty universe raises EmptyUniverseError; invalid lines are excluded from the active
        list, logged as warnings and listed in snapshot.invalid (strict mode raises instead)."""
        strict = self.strict if strict is None else strict
        with self._lock:
            if not self.path.exists():
                raise UniverseError(f"Universe file not found: {self.path}")
            sig = self._signature()
            if self._snap is None or sig != self._sig:
                self._snap = self._load()
                self._sig = sig
            snap = self._snap
        if not snap.tickers:
            raise EmptyUniverseError(f"Universe file {self.path} has no active tickers - refusing to run modules.")
        if snap.invalid:
            msg = f"{len(snap.invalid)} invalid universe line(s): " + "; ".join(
                f"line {e.line_no} {e.raw!r} ({e.reason})" for e in snap.invalid[:5])
            if strict:
                raise UniverseValidationError(msg)
            log.warning(msg)
        return snap

    def _load(self) -> UniverseSnapshot:
        text = self.path.read_text(encoding="utf-8-sig")
        entries = parse_text(text)
        tickers = tuple(e.symbol for e in entries if e.status == "active")
        snap = UniverseSnapshot(tickers=tickers, entries=tuple(entries), source=str(self.path))
        log.info("Universe loaded: %s", snap.summary())
        return snap

    # ------------------------------------------------------------------ admin edits
    def _read_lines(self) -> list[str]:
        return self.path.read_text(encoding="utf-8-sig").splitlines() if self.path.exists() else []

    def _backup(self) -> str | None:
        if not self.path.exists():
            return None
        bdir = self.path.parent / "backups"
        bdir.mkdir(exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        dest = bdir / f"{self.path.stem}.{stamp}{self.path.suffix}"
        shutil.copy2(self.path, dest)
        for old in sorted(bdir.glob(f"{self.path.stem}.*{self.path.suffix}"))[:-_BACKUPS_KEPT]:
            old.unlink(missing_ok=True)
        return str(dest)

    def _write_atomic(self, lines: list[str]) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".universe.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write("\n".join(lines).rstrip("\n") + "\n")
            os.replace(tmp, self.path)           # atomic on Windows and POSIX
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def _state(self) -> dict[str, Entry]:
        """symbol -> first entry (active wins over disabled)."""
        out: dict[str, Entry] = {}
        for e in parse_text("\n".join(self._read_lines())):
            if e.status in ("active", "disabled") and (e.symbol not in out or e.status == "active"):
                out[e.symbol] = e
        return out

    def _commit(self, lines: list[str], rep: ChangeReport) -> ChangeReport:
        with self._lock:
            rep.backup = self._backup()
            self._write_atomic(lines)
            self._snap = None
        try:
            rep.new_version = self.get().version
        except EmptyUniverseError:
            rep.new_version = None
        return rep

    def add(self, symbols: list[str]) -> ChangeReport:
        """Add symbols (bulk or single). Existing / disabled / invalid ones are reported, never silently dropped."""
        rep = ChangeReport()
        with self._lock:
            state, lines, fresh, queued = self._state(), self._read_lines(), [], set()
            for raw in symbols:
                sym = normalize_symbol(raw)
                reason = invalid_reason(sym)
                if reason:
                    rep.invalid.append((str(raw), reason))
                elif sym in queued or (sym in state and state[sym].status == "active"):
                    rep.already_present.append(sym)
                elif sym in state:
                    rep.exists_but_disabled.append(sym)
                else:
                    queued.add(sym); fresh.append(sym)
            if fresh:
                lines += ["", f"# --- added {datetime.now().strftime('%Y-%m-%d %H:%M')} ---"] + fresh
                rep.added = fresh
                return self._commit(lines, rep)
        return rep

    def add_from_text(self, text: str) -> ChangeReport:
        return self.add(parse_bulk(text))

    def _rewrite(self, symbols: list[str], rep: ChangeReport, kind: str) -> ChangeReport:
        targets = {normalize_symbol(s) for s in symbols}
        with self._lock:
            state, out, done = self._state(), [], set()
            for line in self._read_lines():
                hit = None
                for e in parse_text(line):
                    if e.symbol in targets:
                        hit = e
                if hit is None:
                    out.append(line); continue
                if kind == "remove" and hit.status in ("active", "disabled"):
                    rep.removed.append(hit.symbol); done.add(hit.symbol)
                elif kind == "disable" and hit.status == "active":
                    out.append("# " + line.strip().split("#")[0].strip()); rep.disabled.append(hit.symbol); done.add(hit.symbol)
                elif kind == "enable" and hit.status == "disabled":
                    out.append(hit.symbol); rep.enabled.append(hit.symbol); done.add(hit.symbol)
                else:
                    out.append(line)
            rep.not_found = sorted(t for t in targets if t not in done)
            return self._commit(out, rep) if done else rep

    def remove(self, symbols: list[str]) -> ChangeReport:
        return self._rewrite(symbols, ChangeReport(), "remove")

    def disable(self, symbols: list[str]) -> ChangeReport:
        return self._rewrite(symbols, ChangeReport(), "disable")

    def enable(self, symbols: list[str]) -> ChangeReport:
        return self._rewrite(symbols, ChangeReport(), "enable")

    # ------------------------------------------------------------------ publish
    def write_manifest(self, out_path: str | os.PathLike) -> Path:
        """Small JSON describing the universe in use (picked up by the published manifest + Execution Monitor)."""
        snap = self.get()
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({**snap.summary(), "tickers": list(snap.tickers)}, ensure_ascii=False), encoding="utf-8")
        return out
