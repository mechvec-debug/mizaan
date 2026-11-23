from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable


@dataclass(frozen=True)
class Entry:
    line_no: int
    raw: str                 # exactly what was written in the file (identifier preservation)
    symbol: str              # normalised
    status: str              # active | disabled | invalid | duplicate
    reason: str | None = None
    note: str | None = None  # optional inline "# comment"


@dataclass(frozen=True)
class UniverseSnapshot:
    """Immutable view of the universe at one moment. Modules receive this, never the file."""
    tickers: tuple[str, ...]                 # active, de-duplicated, first-seen order
    entries: tuple[Entry, ...]               # every parsed line (for audit / admin screen)
    source: str
    loaded_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))

    # ---- derived -------------------------------------------------------
    @property
    def version(self) -> str:
        """Content version: stable under re-ordering/case/comments; changes when the active set changes.
        Used as the 'relevant data version' in cache keys and written into published manifests."""
        h = hashlib.sha256("\n".join(sorted(self.tickers)).encode()).hexdigest()
        return h[:12]

    @property
    def count(self) -> int:
        return len(self.tickers)

    def _by(self, status: str) -> list[Entry]:
        return [e for e in self.entries if e.status == status]

    @property
    def invalid(self) -> list[Entry]:
        return self._by("invalid")

    @property
    def duplicates(self) -> list[Entry]:
        return self._by("duplicate")

    @property
    def disabled(self) -> list[Entry]:
        return self._by("disabled")

    @property
    def ok(self) -> bool:
        return bool(self.tickers) and not self.invalid

    def subset(self, allowed: Iterable[str]) -> tuple[str, ...]:
        """Active tickers that are also in `allowed` (used by per-module universe filters). Order preserved."""
        a = set(allowed)
        return tuple(t for t in self.tickers if t in a)

    def summary(self) -> dict:
        return {"version": self.version, "active": self.count, "disabled": len(self.disabled),
                "invalid": len(self.invalid), "duplicates": len(self.duplicates),
                "source": self.source, "loaded_at": self.loaded_at}


@dataclass
class ChangeReport:
    """Result of an admin edit - everything that did NOT happen is listed, nothing fails silently."""
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    disabled: list[str] = field(default_factory=list)
    enabled: list[str] = field(default_factory=list)
    already_present: list[str] = field(default_factory=list)
    exists_but_disabled: list[str] = field(default_factory=list)
    not_found: list[str] = field(default_factory=list)
    invalid: list[tuple[str, str]] = field(default_factory=list)     # (raw, reason)
    backup: str | None = None
    new_version: str | None = None

    @property
    def changed(self) -> bool:
        return bool(self.added or self.removed or self.disabled or self.enabled)
