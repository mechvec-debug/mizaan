# Annual Report Intelligence (Phase 8)

Reads each stock's annual report once a year and fills the Shariah evidence store (haram revenue, impermissible interest income,
non-compliant investments) so Criterion 1 and the other pending criteria can be decided. XML first; PDF only as a workaround.

## Before the first run - two settings you must provide
`config/app.yaml -> annual_report`:
* `daily_token_budget`  total tokens (input + output) the batch may use per day
* `requests_per_minute` AI requests per minute
Take both from your AI provider's **current** limits. They are `null` on purpose: **the batch refuses to run until you set them**
(it never guesses a limit and cannot burn a free tier by accident). Also set `GEMINI_API_KEY` in `.env` / GitHub secrets.

## A daily run
1. **Sync** the queue with the universe (new stocks start *unchecked*; removed stocks are marked *removed*, history kept).
2. **Recover** a crashed run (a stock left *processing* longer than 6 h goes back; `--recover-now` for an immediate manual restart).
3. **Look up reports** on BSE for up to 200 stocks (own rate budget, does not use the AI budget). Same report version as already
   analysed = nothing to do; new version = queued; none = re-checked after 30 days; analysed stocks are re-checked after 300 days.
4. **Analyse** pending stocks until one of: `stock_limit` (100/day, hard-coded ceiling) - `token_budget` - `rate_limited` (AI 429:
   the stock goes back untouched, no failure counted) - `completed`.
5. Write the dashboard numbers to `results/annual_report/dashboard.json`.
State is saved after every stock. Tomorrow's run continues where today stopped; nothing finished is redone.
1,228 stocks at 100/day = about 13 days, plus the discovery pace (200 look-ups/day, in parallel) - inside your 10-15 day window.

## Token budget rule
Before each call: `used today + estimated input + max output > budget` -> the call is **not started** (stop reason `token_budget`).
The estimate is deliberately conservative (3 characters per token, full output allowance), so a day normally ends a little below the
budget. After each call the provider's real usage is added; if it overshoots, the batch stops at once. The budget is per calendar
day across all runs. An alert is written at 80 % of the budget and whenever a run stops early.

## What is sent to the AI
Only selected note sections: XBRL text-block notes (up to 32,000 characters), or for the PDF workaround the 8 most relevant pages
(same keywords and cap as the original; ranked by relevance so a table of contents cannot use up the cap). Never a whole report.
The prompt keeps the original's method (Islamic-finance auditor, Ind-AS notes, three figures in INR crore, JSON) with two changes
the strict policy requires: a figure that is not stated is **null** (the original said "assign 0.0"), and every figure must carry its
**evidence** (note/page and the figure as written). A figure without evidence, a negative or non-numeric value is rejected. Nothing
enters the Shariah screener unexplained; null stays "pending".

## Failures
Download or AI-output failures are retried after 1, 3, 7, 14 days; the 5th failure is final (needs an admin: `failed_final`).
Tokens burnt by an unusable answer are counted. A report whose XML has no note text and has no PDF is a failure with that reason.
An analysis that resolves no field at all is saved as `inconclusive` (processed, but the Shariah criteria stay pending; an admin can
enter evidence by hand).

## Saved per analysis (`state/annual_report/analyses/<SYMBOL>.json`)
symbol, company name, fiscal year, report source and version, report date, download date, analysis date, status, AI model (the one
actually used), tokens in/out (and whether estimated), fields written / rejected with reasons, the AI output.

## Dashboard fields (`results/annual_report/dashboard.json`)
total eligible stocks - reports available - processed - pending - failed (with retry date) - no report found - not yet checked -
inconclusive - today's batch (stocks / limit, look-ups, stop reason, alerts) - tokens (today in/out/total, budget, fraction, lifetime,
last 14 days) - last successful processing date - next scheduled batch - estimated days to finish (upper bound).
`available = processed + pending + failed`.

## Not verified live (no network in the build environment)
* the Gemini client and the BSE report look-up/download - the whole batch logic is proven with scripted fakes (34 tests)
* whether the XBRL filings actually carry note text-blocks; if they do not, stocks fall back to the PDF workaround or fail with
  "no usable note text" - the first live run will show which

## Run
`python -m mizan.annual_report run` - one daily batch    `python -m mizan.annual_report status` - dashboard numbers
