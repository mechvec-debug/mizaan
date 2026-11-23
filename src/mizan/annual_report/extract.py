"""Section-level extraction: only the note sections that matter are sent to the AI - never a whole annual report.

XML first (the XBRL filing's text-block disclosures); PDF page selection is the workaround for filings with no usable XML text.
Page selection keeps the original's keywords and its cap of 8 pages, but ranks pages by relevance instead of taking the first 8
matches (the original could spend its whole cap on a table of contents).
"""
from __future__ import annotations

import io
import re

KEYWORDS = ("other income", "borrowings", "segment reporting", "current investments", "interest income")      # as in the original
TAG_HINTS = ("disclosure", "note", "segment", "explanatory", "otherincome", "borrowing", "investment", "interest")


def score_page(text: str, keywords=KEYWORDS) -> float:
    low = text.lower()
    distinct = sum(1 for k in keywords if k in low)
    if not distinct:
        return 0.0
    hits = sum(low.count(k) for k in keywords)
    lines = [l for l in text.splitlines() if l.strip()]
    toc = len(lines) > 25 and sum(1 for l in lines if re.search(r"\s\d{1,3}\s*$", l)) / len(lines) > 0.5      # contents / index page
    return (3 * distinct + min(hits, 10)) * (0.1 if toc else 1.0)


def select_pages(pages: list[str], keywords=KEYWORDS, max_pages: int = 8) -> list[tuple[int, str]]:
    scored = [(score_page(t, keywords), i, t) for i, t in enumerate(pages)]
    top = sorted((x for x in scored if x[0] > 0), key=lambda x: (-x[0], x[1]))[:max_pages]
    return sorted(((i + 1, t) for _, i, t in top), key=lambda x: x[0])                                   # (1-based page no., text) in page order


def pdf_pages(content: bytes) -> list[str]:
    from pypdf import PdfReader
    return [(p.extract_text() or "") for p in PdfReader(io.BytesIO(content)).pages]


def _plain(text: str) -> str:
    try:
        import lxml.html
        return lxml.html.fromstring(f"<div>{text}</div>").text_content()
    except Exception:
        return re.sub(r"<[^>]+>", " ", text)


def sections_from_xbrl(content: bytes, min_chars: int = 200, keywords=KEYWORDS) -> list[str]:
    """Long non-numeric text blocks (the notes) from an XBRL filing, kept when the tag name or the text looks relevant."""
    from lxml import etree
    try:
        root = etree.fromstring(content)
    except etree.XMLSyntaxError:
        return []
    out = []
    for e in root.iter():
        if not isinstance(e.tag, str) or e.get("contextRef") is None:
            continue
        txt = (e.text or "").strip()
        if len(txt) < min_chars:
            continue
        try:
            float(txt)
            continue
        except ValueError:
            pass
        name = etree.QName(e).localname
        plain = " ".join(_plain(txt).split())
        if any(h in name.lower() for h in TAG_HINTS) or any(k in plain.lower() for k in keywords):
            out.append(f"[{name}] {plain}")
    return out


def build_excerpt(sections: list[str], max_chars: int) -> str:
    """Join sections in order until the character cap is reached (the last one is truncated, the rest dropped)."""
    out, used = [], 0
    for s in sections:
        room = max_chars - used
        if room <= 0:
            break
        chunk = s[:room]
        out.append(chunk)
        used += len(chunk) + 2
    return "\n\n".join(out)


def estimate_tokens(text: str, chars_per_token: float = 3.0) -> int:
    return int(len(text) / chars_per_token) + 1
