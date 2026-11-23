"""The audit prompt. Method kept from the original (Islamic-finance auditor, Ind-AS footnotes, three figures in INR crore, JSON).
Two changes the strict policy REQUIRES: 'not stated' is null (the original said 'assign 0.0', turning silence into a number),
and every figure must carry the evidence it came from (so nothing enters the screener unexplained)."""
from __future__ import annotations

FIELDS = ("haram_business_revenue", "impermissible_interest_income", "non_compliant_investments")


def build_prompt(symbol: str, company: str, fy_end: str | None, excerpt: str) -> str:
    return f"""You are an Islamic finance auditor reviewing extracts from an Indian Ind-AS annual report.
Company: {company} ({symbol}). Fiscal year end: {fy_end or 'not known - read it from the report'}.

Extract the following values in INR crore (convert from lakh / million / rupees if the report uses another unit).
STRICT RULES: never estimate. If the extract does not state a figure, return null - null means "not found" and is NOT zero.
Return 0 only when the report explicitly states nil / none. For every non-null value give short evidence: the note or page and
the figure exactly as written.

1. haram_business_revenue - revenue from business activities not permissible under Shariah (alcohol, tobacco, gambling, pork,
   adult entertainment, conventional interest-based financial services, weapons), summed over the segments or products concerned.
2. impermissible_interest_income - interest income and other income earned from interest-bearing deposits, loans or securities.
3. non_compliant_investments - investments in interest-bearing securities and in companies or funds that are not Shariah compliant.

Return strictly one JSON object with this exact schema:
{{
  "fiscal_year_end": "YYYY-MM-DD or null",
  "haram_business_revenue": {{"value": number or null, "evidence": "text"}},
  "impermissible_interest_income": {{"value": number or null, "evidence": "text"}},
  "non_compliant_investments": {{"value": number or null, "evidence": "text"}}
}}

Report extract:
{excerpt}
"""


def validate_output(data: dict) -> tuple[dict, dict]:
    """-> (accepted {field: (value, evidence)}, rejected {field: reason}). A value needs: a number >= 0 and non-empty evidence."""
    ok, bad = {}, {}
    for f in FIELDS:
        item = data.get(f)
        if not isinstance(item, dict):
            bad[f] = "missing"
            continue
        v, ev = item.get("value"), (item.get("evidence") or "").strip() if isinstance(item.get("evidence"), str) else ""
        if v is None:
            bad[f] = "not found in the extract"
        elif isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or v < 0:
            bad[f] = f"invalid value {v!r}"
        elif not ev:
            bad[f] = "value given without evidence"
        else:
            ok[f] = (float(v), ev)
    return ok, bad
