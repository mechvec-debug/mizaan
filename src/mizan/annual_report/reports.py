"""Where the annual report comes from. XML first (the BSE XBRL filing, same source the Shariah screener already uses);
the annual-report PDF is only a workaround for filings that carry no usable note text. A report's VERSION is a content hash
(XML) or attachment name + date (PDF): the same version is never analysed twice, a revised filing is."""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Protocol

from ..core.errors import PermanentProviderError
from ..data.providers.bse import BseXbrlProvider, xml_from_bytes


@dataclass(frozen=True)
class ReportRef:
    kind: str                     # "xbrl" | "pdf"
    attachment: str
    news_date: str
    fy_end: str | None
    version: str
    source: str                   # "bse_xbrl" | "bse_pdf"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ReportRef":
        return cls(**d)


class ReportProvider(Protocol):
    def find(self, symbol: str, bse_code: str | None) -> ReportRef | None: ...
    def find_pdf(self, symbol: str, bse_code: str | None) -> ReportRef | None: ...
    def fetch(self, ref: ReportRef) -> bytes: ...


class BseReportProvider:
    """Not exercised against the live BSE service in the build sandbox (no network)."""

    def __init__(self, bse: BseXbrlProvider, parse):
        self.bse, self.parse = bse, parse

    def find(self, symbol: str, bse_code: str | None, today: datetime | None = None) -> ReportRef | None:
        if not bse_code:
            return None
        today = today or datetime.now()
        for c in self.bse._candidates(bse_code, today)[: self.bse.max_candidates]:
            raw = self.bse.download(c["attachment"])
            xml = xml_from_bytes(raw) if raw else None
            try:
                facts = self.parse(xml) if xml else None
            except Exception:
                facts = None
            if facts and facts.get("annual"):
                return ReportRef("xbrl", c["attachment"], c["news_date"], facts.get("fy_end"), hashlib.sha256(xml).hexdigest()[:16], "bse_xbrl")
        return None

    def find_pdf(self, symbol: str, bse_code: str | None, today: datetime | None = None) -> ReportRef | None:
        if not bse_code:
            return None
        accept = lambda att, subj, head: att.lower().endswith(".pdf") and "ANNUAL REPORT" in subj + head
        for c in self.bse._candidates(bse_code, today or datetime.now(), accept)[:1]:
            ver = hashlib.sha256(f"{c['attachment']}|{c['news_date']}".encode()).hexdigest()[:16]
            return ReportRef("pdf", c["attachment"], c["news_date"], None, ver, "bse_pdf")
        return None

    def fetch(self, ref: ReportRef) -> bytes:
        raw = self.bse.download(ref.attachment)
        if raw is None:
            raise PermanentProviderError(f"attachment {ref.attachment} not found on BSE")
        return (xml_from_bytes(raw) or b"") if ref.kind == "xbrl" else raw
