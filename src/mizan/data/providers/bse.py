"""BSE corporate-filing adapter: finds the latest ANNUAL XBRL filing of a scrip and returns its extracted facts.

Ported from the original downloader (same endpoints and headers) with two differences:
  * it returns parsed facts, not files - raw XML is never stored in the repo (re-downloadable, would add gigabytes)
  * it skips quarterly / half-yearly filings: the original took the first XBRL attachment it saw, whatever period it covered
NOT exercised against the live BSE service in the build sandbox (no network) - verify with scripts/verify_bse_live.py.
"""
from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timedelta

from ...core.errors import RateLimitError, TransientProviderError

BSE_API_URL = "https://api.bseindia.com/BseIndiaAPI/api/AnnGetData/w"
BASE_URLS = ["https://www.bseindia.com/xml-data/corpfiling/AttachLive/", "https://www.bseindia.com/xml-data/corpfiling/AttachHis/"]
HEADERS = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
           "Referer": "https://www.bseindia.com/corporates/ann.html", "Origin": "https://www.bseindia.com",
           "Accept": "application/json, text/plain, */*", "Accept-Language": "en-US,en;q=0.9"}


def _json(res) -> dict:
    """BSE answers with single- or double-encoded JSON."""
    try:
        d = res.json()
        if isinstance(d, str):
            d = json.loads(d)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def xml_from_bytes(data: bytes) -> bytes | None:
    if zipfile.is_zipfile(io.BytesIO(data)):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for name in z.namelist():
                if name.lower().endswith((".xml", ".xbrl")):
                    return z.read(name)
        return None
    return data


class BseXbrlProvider:
    name = "bse"

    def __init__(self, parse, session=None, max_pages: int = 4, max_candidates: int = 8, timeout: float = 25.0):
        self.parse, self._session, self.max_pages, self.max_candidates, self.timeout = parse, session, max_pages, max_candidates, timeout

    def provider_symbol(self, ticker: str) -> str:
        return ticker

    def _http(self):
        if self._session is None:
            import requests
            self._session = requests.Session()
        return self._session

    def _get(self, url, **kw):
        import requests
        try:
            res = self._http().get(url, headers=HEADERS, timeout=self.timeout, **kw)
        except requests.RequestException as exc:
            raise TransientProviderError(f"BSE request failed: {exc}") from exc
        if res.status_code == 429:
            raise RateLimitError("BSE rate limit (429)")
        return res

    @staticmethod
    def _is_xbrl(att: str, subj: str, head: str) -> bool:
        return att.lower().endswith((".xml", ".xbrl")) or ("XBRL" in subj + head and att.lower().endswith(".zip"))

    def _candidates(self, scrip: str, today: datetime, accept=None) -> list[dict]:
        accept = accept or self._is_xbrl
        out = []
        for page in range(1, self.max_pages + 1):
            res = self._get(BSE_API_URL, params={"pageno": page, "strCat": "-1", "strPrevDate": (today - timedelta(days=365)).strftime("%Y%m%d"),
                                                 "strScrip": str(scrip).strip(), "strSearch": "P", "strToDate": today.strftime("%Y%m%d"),
                                                 "strType": "C", "subcategory": ""})
            if res.status_code >= 500:
                raise TransientProviderError(f"BSE HTTP {res.status_code}")
            rows = _json(res).get("Table") or _json(res).get("Table1") or []
            for r in rows:
                att = (r.get("ATTACHMENTNAME") or "").strip()
                subj, head = (r.get("NEWSSUB") or "").upper(), (r.get("HEADLINE") or "").upper()
                if att and accept(att, subj, head):
                    out.append({"attachment": att, "news_date": r.get("NEWS_DT") or r.get("DissemDT") or "", "subject": subj[:120]})
        return out

    def download(self, attachment: str) -> bytes | None:
        """Bytes of a filing attachment (tries the live then the historical folder); None if neither has it."""
        for base in BASE_URLS:
            res = self._get(base + attachment)
            if res.status_code == 200:
                return res.content
        return None

    def fetch_xbrl_facts(self, bse_code: str, today: datetime | None = None) -> dict:
        today = today or datetime.now()
        cands = self._candidates(bse_code, today)
        tried = 0
        for c in cands[: self.max_candidates]:
            for base in BASE_URLS:
                res = self._get(base + c["attachment"])
                if res.status_code != 200:
                    continue
                xml = xml_from_bytes(res.content)
                tried += 1
                try:
                    facts = self.parse(xml) if xml else None
                except Exception:
                    facts = None
                if facts and facts.get("annual"):
                    return {"found": True, "filing": c, "facts": facts, "checked_at": today.strftime("%Y-%m-%d")}
                break
        return {"found": False, "candidates": len(cands), "parsed": tried, "checked_at": today.strftime("%Y-%m-%d")}
