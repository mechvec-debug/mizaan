# Shariah Compliance Screener (Screener 3) — Phase 7

## Policy: no assumption, no exception
The six criteria and their thresholds are exactly the original's (C1 haram revenue < 5 %, C2 impermissible income < 5 %,
C3 debt/assets < 33 %, C4 illiquid/assets ≥ 20 %, C5 net liquid assets < market cap, C6 non-compliant investments/assets < 33 %).
What changed is how missing data is treated. Each criterion is **True / False / undetermined (None)**:

| Verdict | Rule |
|---|---|
| NON_COMPLIANT | at least one criterion is a breach (a breach stands even if other data is missing) |
| COMPLIANT | all six criteria are True |
| UNDETERMINED | no breach, but at least one criterion cannot be decided yet - the record lists which and why |

A value that is **not reported is unknown, never 0**. No sector shortcut, no stock-specific override, no default.
Bounds decide a criterion only when they are rigorous: e.g. *total investments* is an upper bound of *non-compliant investments*,
*current assets* is an upper bound of *liquid assets*.

## What to expect from the first run (important)
**C1 (core business) needs the haram-revenue figure, which no statement contains.** Until it is established from the annual
report (Phase 8) or entered by an admin with a written justification, **no stock can be COMPLIANT** — most will show UNDETERMINED
("Criteria 1 pending"). Stocks that already breach C2, C3, C4 or C5 from their own numbers are NON_COMPLIANT now (banks and lenders
fall here through Criterion 2: interest income counts as impermissible income). This is the intended behaviour of "no assumptions",
and the old 811 "compliant" results were not verified by this standard (C1 was fixed at 0 %).

## How a verdict is built
1. `info` (market cap, sector) and `statements` come from the **shared cache** — the same blobs the Fundamental screener uses, so running both costs no extra Yahoo calls.
2. Optional annual XBRL (needs a BSE code, `data/universe/nse_bse_map.csv`) through the same cache/ledger. Raw XML is never stored; extracted facts are.
3. **One source, one fiscal year per verdict** — the latest fiscal year wins, XBRL wins a tie; fields are never mixed across sources or years.
4. Evidence (`data_store/state/shariah_evidence/<TICKER>.json`) is applied only if it is for the same fiscal year; otherwise it is reported as ignored.
5. One evaluation function serves the daily batch, the dashboard and future modules (the original had two diverging copies).

Canonical definitions — illiquid = PPE + CWIP + investment property + intangibles + inventories; liquid = cash + bank balances + receivables + other current financial assets; debt = borrowings (XBRL) / "Total Debt" (Yahoo).

## Original weaknesses fixed (all documented, none silent)
| Original | Now |
|---|---|
| C1 fixed at 0 % for every stock, ITC 75 % | C1 undetermined until evidence; no stock named in the code |
| bank / financial shortcut (C1 = 100 %) | judged on the six criteria (interest income → C2) |
| missing values defaulted to 0 | unknown stays unknown |
| "current investments" treated as non-compliant | total (current + non-current) investments used only as an upper bound; exact value needs evidence |
| `value > 1e7 ⇒ ÷1e7` unit guess | unit read from the filing; unknown unit blocks only C5 (ratios are scale-free) |
| last numeric node in the XML (any year, any segment) | current-year, non-dimensional context only |
| total liabilities = `EquityAndLiabilities` (= total assets) | `Liabilities` tag, or EquityAndLiabilities − Equity; Yahoo: reported row or identity |
| first XBRL filing in 365 days (could be quarterly) | only annual filings (period ≥ 330 days) |
| Yahoo rows from different fiscal years mixed | every value from the same fiscal-year column |
| Goodwill aggregate + its components + "Leases" all added to illiquid | aggregate row only (components only if no aggregate); Leases dropped (already inside Net PPE) |
| "Net non-operating interest" used as interest income | gross interest rows only |
| non-current liabilities used as a total-liabilities fallback | removed |
| C2/C6: audit value used only if non-zero | evidence and statement figures are compared; the larger interest figure rules |
| app and watchdog gave different verdicts | one path |
| 1,223 of 1,228 stocks had no BSE code | ISIN-based map builder (`scripts/build_bse_map.py`) |

## Evidence: closing C1 / C2 / C6
`python -m mizan.screeners.shariah.evidence_cli set TCS haram_business_revenue 0 --fy 2026-03-31 --evidence "Segment note 38: single IT-services segment"`
Fields (Rs crore): `haram_business_revenue`, `impermissible_interest_income`, `non_compliant_investments`. A figure without a source, fiscal year and justification is refused.
Phase 8 (annual-report analysis, ≤ 100 stocks/day, paced for the free tier) writes the same store.

## Output (original schema + additive fields)
`data_store/results/shariah/latest_results.json` → `{last_updated, data:[{ticker, bse_code, sector, market_cap_cr, financial_date, is_compliant, failure_reason, metrics{6×(metric, threshold, status)}, verdict, breached[], undetermined[{criterion, reason}], data_source, fiscal_year_end, xbrl_scale, evidence_used[], evaluated_at}]}`.
`status` is true / false / **null** (undetermined). `is_compliant` is true only for COMPLIANT. `run_report.json` lists failures, carried-forward rows (annual screen: a previous verdict is kept up to 30 days, dated, if today's data is unavailable), per-stock data notes and the count of stocks pending each criterion.

## Run it
```
python scripts/build_bse_map.py EQUITY_L.csv ListOfScrips.csv      # once (and when IPOs are added)
python -m mizan.screeners.shariah
python scripts/cross_check.py                                       # Technical + Fundamental + Shariah side by side
```

## Proof and limits
Automated, offline: the 15 decidable boundary cases of the original engine reproduce status, metric text and threshold exactly; strict-policy, bounds, XBRL-parser, bundle, evidence, mapper, BSE-adapter and scanner behaviour are tested; an end-to-end test runs all three screeners through one Data Manager and shows each dataset is downloaded once for all three.
**Not verified live (no network in the build environment):** the BSE adapter, the BSE/NSE master-file column names, and which XBRL tags real filings use. `python scripts/verify_bse_live.py` runs the adapter and cross-checks the filing's total assets against Yahoo's — please run it and send me the output.
