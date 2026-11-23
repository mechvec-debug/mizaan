# Quality Value Rebound (Screener 2, ex "Deep-Value Sentinel") — Phase 6

## What it does (unchanged)
Finds strong businesses at a sensible price, 35-65 % below their 3-year high, near the 52-week low, with a higher low and
daily momentum turning up (short/mid-term trade). All thresholds, formulas, statuses, sort order and the 23 output columns are
exactly the original's (`docs/BUSINESS_RULES.md` section 1). The calculation code in `logic.py` is a verbatim port.

## What changed (integration only)
| Original | Now |
|---|---|
| tickers from Google Sheet tab `Watchlist` (344) | the single universe file (`UniverseService`), currently 1,228 |
| Yahoo calls inside the scanner, 1.2 s pause per ticker | all data through the shared `DataManager` (cache, batch history, throttle, ledger) |
| results written to a Google Sheet **and** `data.json` | `data.json` (same format) + `run_report.json` in `data_store/results/fundamental/`; the Sheet is no longer written (publishing is Phase 10) |
| run ends with only a printed summary | `run_report.json`: counts, failures, skips, degradations, carried-forward rows, data-run id, universe version |

**Heads-up:** the old Streamlit `app.py` reads the Google Sheet, which this module no longer updates. The new dashboard (Phase 9) reads the published JSON.

## Behaviour kept from the original
* one bad ticker never stops the run; fewer than 120 daily bars → ticker skipped
* statements that cannot be read are treated as empty and the ticker is still evaluated (it will normally land in WAIT (Fund)) — but it is now **listed** under `degraded_detail`
* nothing produced → previous `data.json` is left untouched

## New, so nothing is silent
* A ticker whose data is unavailable today **keeps its previous row** for up to 7 days; its `Updated` column keeps the old time, so it can never pass as fresh. Listed under `carried_forward_detail`; after 7 days it drops out.
* A stock whose last price bar is more than 7 days old (suspended/delisted) is **not published**; listed under `failed_detail` as `stale_price`.
* Rate limiting stops the run cleanly (circuit breaker); remaining tickers are `deferred` and keep their previous rows; the next run resumes from the cache.
* A stock removed from the universe disappears from the results (single source of truth).

## Proof of parity (automated, runs offline)
* 18 fixture scenarios produced by the **original** `scan_ticker` (all five statuses) reproduced exactly through the full Data Manager path
* **400 randomised cases** run through the original and the port: identical output on every case (WAIT Fund 286, WATCH 67, WAIT Price 37, WAIT Valuation 10; PASS is covered by the fixture scenario)
* `technicals()` replayed against saved original output to 9 decimals
Live check on your machine: `python scripts/verify_fundamental_live.py`

## Cost to expect
1,228 stocks → about 1,228 Yahoo `info` calls per day (prices move daily) at the configured 30/min ≈ 41 min, plus a one-off ~3,700 calls the first time (statements); batch history is ~25 calls. Statements refresh weekly. All resumable: a rate-limited day just continues the next day.

## Run it
`python -m mizan.screeners.fundamental` (live Yahoo; writes to `data_store/results/fundamental/`)
