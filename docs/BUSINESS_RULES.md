# Business Rules & Baseline (Phase 2)

Status legend: **PRESERVE** = reproduce exactly · **CHANGE** = approved deviation (Section 5) · **PROPOSE** = needs your approval.
Sources: `original/` (unmodified except one redacted secret). Baselines: `golden_master/`.

---
## 1. Screener 2 — Fundamental (Deep-Value Sentinel)  → PRESERVE (all)
Source: `original/fundamental/dashboard_worker.py`. Every number below is the code value.

| Group | Rule |
|---|---|
| Universe input | **CHANGE:** ticker list now from the single universe file (was Google Sheet `Watchlist`). `.NS` appended as before. |
| Data | Yahoo `info`, `history("3y")`, `income_stmt`, `balance_sheet`. Skip ticker if < 120 daily bars after download (tested at 119 → skipped, 120 → scanned). |
| Fundamentals (ALL must pass) | MCap(`marketCap/1e7`) **> 1000**; Sales growth(`revenueGrowth×100`) **> 8**; OPM(`operatingMargins×100`) **> 8**; ROE **> 10** = NetIncome/Equity×100 (fallback `returnOnEquity×100` only if Equity is 0); ROCE **> 10** = EBIT/(Assets − CurrentLiab)×100 (0 if denominator 0); EPS ok = `trailingEps>0` and `≥` latest annual Diluted/Basic EPS (fallback `earningsGrowth>0` if no annual EPS); Debt ok = Financial Services sector **or** `debtToEquity/100 ≤ 1.0`. |
| Valuation | `0 < PE ≤ 35` (trailingPE, else forwardPE) **and** (PEG ≤ 0 or PEG ≤ 2.0). Unknown PEG is not penalised. |
| Price zone | drawdown = (1 − price / max(High over window)) × 100. PASS zone **35 ≤ dd ≤ 65**. |
| Technical (daily) | EMA20/50 (span, adjust=False); MACD 12/26/9; Wilder RSI14; ATR14 = **simple** rolling mean; 52-week low = min(Low, last 252); bounce = (price/52wLow − 1)×100; near_bottom = bounce ≤ 35; higher_low = min(Low last 15) > min(Low[-90:-15]). Five momentum checks: price>EMA20 and EMA20>EMA20[-5]; MACD>signal and hist ≥ hist[-3]; 45≤RSI≤70 and RSI>RSI[-5]; mean vol(10) ≥ mean vol(50); price>EMA50. `tech_score` = count; `momentum_ok = ema ∧ macd ∧ score ≥ 4`. |
| Trade plan | stop = 0.98 × min(Low last 15); T1 = max(max High last 60, price + 2·ATR); T2 = max(T1, 52wLow + 0.5·(windowHigh − 52wLow)); RR = (T1 − price)/(price − stop), 0 if risk ≤ 0. |
| Status waterfall | 🔥 PASS = fund ∧ val ∧ in price zone ∧ near_bottom ∧ higher_low ∧ momentum_ok ∧ RR ≥ 1.5 → 👀 WATCH = fund ∧ val ∧ dd ≥ **25** → WAIT (Price) = fund ∧ val → WAIT (Valuation) = fund → WAIT (Fund). |
| Sort | Status order [PASS, WATCH, WAIT Price, WAIT Valuation, WAIT Fund], then Tech Score descending. |
| Output (23 columns, in order) | Ticker, Market Cap, Current price, PE, OPM, Sales growth, ROE, ROCE, EPS current, Volume, RSI, Status, Sector, Drawdown %, Above 52W Low %, Higher Low, Tech Score, Stop Loss, Target 1, Target 2, Risk Reward, Notes, Updated. Values rounded to 2 dp. |
| Dashboard | Two columns: [Execution Ready · Waiting for Price · Waiting for Valuation] and [Watchlist · Fundamental Wait]; column subsets `trade`, `basic(+Drawdown / +PE)`, `watch`, `why`. |
| Documented inconsistencies (kept) | Comments/captions say "50-70% off the high" and "WATCH ≥ 40%"; code uses 35-65 and 25. **Code values rule.** Caption text on the dashboard is display copy and will be corrected only if you approve (PROPOSE-4). |
Baseline evidence: 344 golden rows (30 WATCH / 11 WAIT Price / 54 WAIT Valuation / 249 WAIT Fund / **0 PASS**); 18 synthetic scenarios reaching all five statuses; `technicals()` replay on 3 price shapes.

## 2. Screener 3 — Shariah  → STRICT (see Section 5 for the removals)
Source: `original/shariah/constants.py`, `engine.py`, `xbrl_parser.py`.

| Criterion | Rule (exact) | Status in new build |
|---|---|---|
| C1 Core business | haram revenue / total revenue × 100 **< 5.0** | PRESERVE rule; **CHANGE** input: real segment data, no default 0, no ITC override |
| C2 Impermissible income | impermissible income / total revenue × 100 **< 5.0** (audit value, else financials value) | PRESERVE |
| C3 Debt | interest-bearing debt / total assets × 100 **< 33.0** | PRESERVE |
| C4 Illiquid | illiquid / total assets × 100 **≥ 20.0** | PRESERVE |
| C5 Net liquid | (liquid assets − total liabilities) **<** market cap (₹ Cr) | PRESERVE |
| C6 Non-compliant investments | non-compliant investments / total assets × 100 **< 33.0** | PRESERVE |
| Verdict | compliant = all six pass. Guard: assets ≤ 0, revenue ≤ 0 or NaN → not compliant ("Incomplete Statements"). | **CHANGE:** guard becomes **UNDETERMINED** (approved) |
| Output schema | `{last_updated, data:[{ticker, bse_code, sector, market_cap_cr, financial_date, is_compliant, failure_reason, metrics{6×(metric, threshold, status)}}]}` | PRESERVE + additive fields (`verdict`, `undetermined_reasons`, `data_source`, `evidence`) |
| Dashboard | dark theme, 4 KPI cards (Total / Compliant / Non-Compliant / Rate), expander per stock with 2×3 criteria cards, ✅/❌ badges | PRESERVE; add an UNDETERMINED card/badge |
Baseline evidence: 19 engine boundary cases (exact 5.0 / 33.0 / 20.0 / net-liquid = mcap), 6 parser cases, 1,228 golden rows (811 compliant).

## 3. Screener 1 — Technical  → PRESERVE the Pine core; see Section 4
Pine source: `original/specs/Indicator_backup.pine.txt`. The dashboard filter logic is baselined (217 rows → 10 shown).

**Signal definition (Pine, long-only):**
1. Swing engine: ATR(14); pivots `pivotHigh/Low(left 8, right 3)`; a swing is kept only if its move ≥ 0.7 × ATR; last 30 pivots.
2. Structure: CHoCH state machine with protected swings (trend flips only when the swing that launched the last impulse breaks).
3. Leg score = 0.5·Efficiency + 0.5·Displacement-percentile (0-100). With-trend leg with score ≥ **55** = impulse; counter-trend = correction.
4. Setup = latest **upward** impulse: zone = 0.382-0.618 retracement; chart SL = start − 0.2·ATR; chart TP1/TP2 = 1.272/1.618 extension; state machine wait → in-zone → target/stopped (latched).
5. MTF HUD: higher timeframes by CHoCH bias + ADX(14) strength scaled (ADX 10→0, 50→100); verdict ALIGNED / BIAS / MIXED; plan text; low-conviction when mean strength of voting TFs < 20.
6. **Trade trigger:** `EMA9 crosses over EMA21` **on this bar** AND zoneSide = long AND price inside zone.
7. **Trade levels:** entry = close; risk = ATR(14) × 2.0; SL = entry − risk; TP1/2/3 = entry + risk × 1 / 2 / 3. (All 217 golden rows satisfy this exactly.)

**Input/Output parity target:** `ticker, action, entry, sl, tp1, tp2, tp3, aligned, plan, time_received`; dashboard filters: hide FORMING|VOID; latest date only; Signal / MTF / Trade-plan multiselects.

## 4. Technical review — items that look unrealistic for a short-to-mid-term, bullish/sideways goal (PROPOSE; defaults keep original)
| # | Observation | Proposal (all parameterised, default = original) |
|---|---|---|
| T1 | A same-bar EMA9/21 crossover **and** price inside the Fib zone is a rare coincidence on daily bars → few `BUY` signals; the stored states (FORMING / IN BUY ZONE / WAIT) show the real pipeline works around this. | `cross_lookback_bars` (default 1 = exact original). Raising it to e.g. 3-5 treats a recent cross as valid. |
| T2 | Two different stop/target systems coexist: chart (Fib-based) vs alert (ATR-based). The alert is what you store. | Keep ATR-based as the published values; expose Fib levels as optional extra columns. |
| T3 | `FORMING/IN BUY ZONE/WAIT/aligned/plan` strings are **not produced by the supplied Pine** (alert only sends BUY). 5 rows dated 2026-10-30 use the Pine HUD wording ("low conviction — wait"); older rows use different wording ("CAUTION: Low Conviction", "AVOID: Macro Downtrend", "MIXED — WEAK EDGE"). | The Python port will define each state explicitly from Pine variables (`zState`, `priceInZone`, `validBuy`, `verdict`, `plan`) and list the exact mapping for your approval before Phase 5 starts. |
| T4 | Daily chart has only 2 higher TFs (1W, 1M) → ties give "no edge" often; rows like `IN BUY ZONE` with `ALIGNED ▼ SHORTS` appear (plan = AVOID). | No logic change; dashboard default for *Trade Plan* excludes "AVOID" (user can re-tick) — UI default only. Needs your OK. |
| T5 | Pivots confirm 3 bars late; latest structure can refine (documented in Pine as non-repaint note). | Disclose in dashboard footnote; no logic change. |
| T6 | Engine state depends on long history (pivot array, CHoCH, percentile ranks). | Store full daily OHLCV per ticker and **append each new day** (your requirement); rebuild state from stored history. |

**Indicator add/remove (your requirement):** an `indicators.yaml` registry. Each entry: `name, enabled, params, outputs[], usable_as_filter`. Shipped entries mirror the Pine inputs: `ema_fast(9) ema_slow(21) atr_sl(14, ×2.0) rr1/2/3(1/2/3) atr_swing(14, ×0.7) pivots(8,3) impulse_min(55) adx(14)`. Optional extras (RSI, MACD, volume ratio, …) ship **disabled**. Disabling a *core* signal indicator is blocked (it would change the strategy); non-core ones are freely toggled.

## 5. Change register (everything that will differ from the originals)
| ID | Module | Change | Reason | Approved |
|---|---|---|---|---|
| C-1 | All | Single universe file replaces Sheet / tickers.txt / alert-driven list | Your decision | ✅ |
| C-2 | Shariah | Remove ITC 75 % override | "No assumption, no exception" | ✅ |
| C-3 | Shariah | Remove C1 = 0 default; C1 from real segment data | same | ✅ |
| C-4 | Shariah | Remove bank/financial fast-reject (judge on 6 criteria) | same | ✅ (confirm: banks will mostly be UNDETERMINED because bank XBRL tags differ) |
| C-5 | Shariah | Third verdict UNDETERMINED + reasons; never a silent default | Your approval | ✅ |
| C-6 | Shariah | Replace `val>1e7 ⇒ /1e7` guess with XBRL unit/scale; fix `total_liabilities` tag order | Data correctness | ✅ |
| C-7 | Shariah | One shared evaluation path (app and watchdog diverged) | Consistency | ✅ |
| C-8 | Shariah | NSE→BSE mapping built by ISIN so XBRL is actually downloaded | 1,223/1,228 had none | ✅ |
| C-9 | Technical | Python port of Pine + state mapping + indicator registry | Your request | ✅ decisions D1-D9 in docs/TECHNICAL_STATES.md |
| C-11 | Technical | cross_lookback_bars 3, A/B/C grading + ranking, changed-today tracking, stale rows never graded (additive columns only; the 10 original columns unchanged) | Practical observation: only 26% of rows shown today were clean aligned-long setups | ✅ (delegated) - thresholds are starting values for the backtest module to calibrate |
| C-10 | Annual report | XML-first, PDF flagged fallback; paced queue | Your decision | ✅ |
| C-12 | Fundamental | Tickers from the single universe (was Google Sheet); data via the Data Manager; Google Sheet output replaced by data.json + run report; carry-forward / stale-price / degraded reporting added; business rules untouched | Your decisions + "never silent" | ✅ (Phase 6, parity proven) |
| C-13 | Technical | Prices are RAW (split-adjusted, not dividend-adjusted): entry/SL/TP are real tradable prices, as on TradingView. The partial-bias wording ("▲ BULLISH BIAS" / "▼ BEARISH BIAS") is left as implemented (chat decision D4 ignored). `notify_price` added to each record's `details` (zone top while waiting, current close once in the zone / triggered) | Your decisions (D1 raw, D4 ignore, notification price) | ✅ |
| C-14 | Shariah | Three-valued verdict (COMPLIANT / NON_COMPLIANT / UNDETERMINED); unreported = unknown; ITC override, C1 = 0 default, bank shortcut, unit guess removed; rigorous bounds for C3-C6; period/unit-aware XBRL; one shared evaluation path; evidence store with mandatory justification; ISIN-based BSE map. Thresholds and metric wording unchanged. See docs/SHARIAH.md | Your instruction: "no assumption and exception", "undetermined is ok", banks judged by interest income | ✅ (Phase 7) |
| C-15 | Annual report | Prompt keeps the original method (Gemini 2.5 Flash, JSON, 3 figures in INR crore, same keywords, 8-page cap) but a figure not stated is null, not 0.0, and every figure needs evidence; pages ranked by relevance instead of first-8 matches; failures are recorded, never silent zeros | Strict "no assumption" policy | ✅ |
| C-16 | Data layer | Rate limiting counts real HTTP requests (statements 3, info 2, a 50-stock batch 50, BSE look-up 6) and each source has its own budget; default 30 slots/min stays under the observed ~2,000/hour Yahoo tolerance | Your yfinance limit input | ✅ |
| C-17 | Dashboard | Screener pages keep their original designs; Fundamental caption corrected to the code values (35-65 % / 25 %); Shariah page gains an Undetermined card/badge and PENDING rule state; Shariah run button + PDF uploader replaced by admin run + evidence form; sidebar watch-list editor moved to System Configuration (single universe); Technical page gains the A/B and C tabs with the notification price; all admin controls need login (hashed password, fails closed) | Phase 9 requirements + your decisions | ✅ |
| C-18 | Execution / go-live | Sequential execution manager (retry, resume, run lock, run-time budget, persistent job state, alerts) + GitHub workflows saving all data to the `data` branch + validated versioned JSON for other front ends. Parallel mode and a Firestore mirror are deferred (not needed to go live) | Your go-live request | ✅ |
| P-1…P-4 | see Sections 1, 4 | T1, T4, caption text | — | ✅ T1/T4 confirmed; Fundamental dashboard renamed 'Quality Value Rebound' |

## 6. Operating requirements captured from you in this phase (inputs to Phase 3-4, 8)
* **Technical history:** keep each ticker's daily OHLCV permanently; each run downloads only the new day(s) and appends (incremental update, gap detection, split/bonus adjustment check).
* **Annual-report AI pacing:** Shariah is an annual check, so a 10-15 day catch-up is acceptable. Queue state persists; daily cap ≤ 100 stocks; per-minute and per-day request/token caps configurable from the provider's *current* free-tier limits; HTTP 429 → exponential back-off then **pause until the next scheduled run** (not fail); only a new report version re-queues a stock. 1,228 stocks ÷ 100/day ≈ 13 days.
* **Watch point (token budget):** the daily budget is a config value; an alert is written to the Execution Monitor when 80 % is used and when a run stops early.
* **Publishing:** results are written once as versioned static JSON + `manifest.json`; Streamlit, Cloudflare Pages, Firebase and the Android app are thin readers.
* **Admin:** Sami, login = mechvec@gmail.com, password stored as a salted hash (never plaintext in the repo).
