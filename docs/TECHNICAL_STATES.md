# Swing Setup Radar (Screener 1) - Port Specification for Approval (Phase 5, step 1)

No code is written until you approve Section 6. Sources: `original/specs/Indicator_backup.pine.txt` (Pine v6) and the 217
stored alerts in `golden_master/original_outputs/technical_trading_alerts.json`. `nse_scanner.py` was never supplied, so
anything marked **INFERRED** comes from the stored data, not from code.

## 1. What the evidence shows
| Finding | Evidence |
|---|---|
| The Pine script only alerts `BUY` with ticker/entry/sl/tp1-3. The stored rows also carry a state, `aligned` and `plan`, so a Python scanner produced them. | alert JSON in the Pine file vs. 217 rows |
| One row per ticker, every row has entry/SL/TP1-3. | 217 unique tickers; all rows satisfy entry-SL = risk, TPn = entry + n x risk |
| The first scan visited ~204 tickers in ~34 min (one per ~10 s). Later runs update only tickers whose state changed. | `time_received` 2026-07-03 12:19-12:53; 2026-07-07; 2026-10-30 |
| `aligned` <-> `plan` are locked together: ALIGNED LONGS -> BUY pullbacks / low conviction - wait; ALIGNED SHORTS -> AVOID: Macro Downtrend; MIXED -> CAUTION: Low Conviction. | cross-table of all 217 rows (no exceptions) |
| "low conviction - wait" appears only in the newest rows (30 Oct). The July scan did not have that rule. | all 5 such rows are 30 Oct |
| Dashboard hides `FORMING` and `VOID`. No `VOID` or `TARGET` row exists in the data. | dashboard.py + golden data |

## 2. Faithful port - calculation spec (Pine -> Python, daily bars only)
All numbers are the Pine defaults; every one is editable in `config/indicators.yaml`.
1. **ATR** = Wilder RMA of True Range, length 14 (`ta.atr`). RMA seeded with the SMA of the first 14 TRs, as Pine does.
2. **Pivots** `pivothigh(high, 8, 3)` / `pivotlow(low, 8, 3)`; a pivot is stored only if it differs from the previous stored pivot
   by >= 0.7 x ATR; consecutive same-type pivots keep the more extreme one; keep the last 30 pivots.
3. **CHoCH trend** with protected swings (re-computed over the stored pivots, exactly as the draw block does).
4. **Leg score** = 100 x (0.5 x efficiency + 0.5 x displacement percentile); a **with-trend upward leg with score >= 55 is an impulse**.
   Only the **last upward impulse** drives the setup (long-only).
5. **Setup** from that impulse: zone = 0.382-0.618 retracement; chart stop = impulse start - 0.2 x ATR; chart TP1 = +0.272 x range
   above the impulse high. Setup state: wait -> in-zone -> **target hit** (high >= chart TP1) or **stopped** (low <= chart stop).
   Target/stopped **latch** until a new impulse replaces the setup. Draw-block logic is re-run only on a new pivot or the last bar,
   and the latch is updated on every bar - replicated bar by bar.
6. **MTF** (daily chart -> **2 higher timeframes: weekly and monthly**; Pine's auto-ladder has no third): weekly/monthly bars are
   built from daily bars; each timeframe's CHoCH bias and ADX(14) strength (floor 10, ceiling 50 -> 0-100) are taken from the
   **last completed** higher-timeframe bar (Pine returns `t[1]`, `adx[1]`) - the current week/month never leaks in.
   `biasDir` = majority of the two; with 2 timeframes a 1-1 split = 0 (mixed). `conviction` = mean strength of the TFs voting with `biasDir`;
   `lowConv` = biasDir != 0 and conviction < 20.
7. **Trade trigger (`validBuy`)** = EMA(9) crosses over EMA(21) on the latest bar AND a long setup exists AND close is inside the zone.
   With `cross_lookback_bars = N` (default 1 = original): a cross on any of the last N bars, with the other two conditions on the latest bar.
8. **Levels (every row, every state):** entry = latest close; risk = 2.0 x ATR(14); SL = entry - risk; TP1/2/3 = entry + risk x 1/2/3;
   rounded to 2 decimals ("#.##").

## 3. State definitions (what `action` will say)
| `action` text (production wording) | Definition (Pine variables) | Confidence |
|---|---|---|
| 🟢 BUY TRIGGERED | `validBuy` on the latest bar | High (4 July rows; rule is in the Pine) |
| 🔵 IN BUY ZONE | long setup exists, `zState == 1` (close inside zone, not resolved) | High (Pine chip "IN BUY ZONE") |
| 🟡 WAIT FOR BUY ZONE | long setup exists, `zState == 0` (close outside zone, not resolved) | High (Pine chip "WAIT FOR BUY ZONE") |
| ⚪ FORMING | **no qualified upward impulse yet** (`zoneSide == 0`, Pine chip "Trend - no setup") | **INFERRED** - 141 of 217 rows (65%) |
| VOID | setup **stopped** (`zState == 3`, Pine chip "STOPPED - setup void") | Medium - never appears in your data |
| TARGET HIT | setup reached chart TP1 (`zState == 2`, Pine chip "TARGET HIT - done") | Medium - never appears in your data |

Default dashboard visibility is identical to today: **FORMING and VOID hidden**; I propose TARGET HIT hidden too (it is "done", not actionable).
If the original scanner folded stopped/target rows into FORMING, nothing visible changes either way.

## 4. `aligned` and `plan`
| Condition (2 higher TFs) | `aligned` (production wording) |
|---|---|
| both bullish | ALIGNED ▲ LONGS |
| both bearish | ALIGNED ▼ SHORTS |
| 1 bull : 1 bear | MIXED — WEAK EDGE (Pine says "NO EDGE"; your data says "WEAK EDGE" -> keep yours) |
| one trend, one flat | ▲ BULLISH BIAS / ▼ BEARISH BIAS (Pine wording; never seen in your data) |
| none available | NO HIGHER TF |

| Condition | `plan` |
|---|---|
| biasDir > 0 and not lowConv | BUY pullbacks |
| biasDir > 0 and lowConv | low conviction — wait |
| biasDir < 0 | AVOID: Macro Downtrend (long-only: "SELL rallies" is meaningless) |
| biasDir == 0 | CAUTION: Low Conviction |

## 5. Output, universe, config
* Record = your 10 original fields (`ticker, action, entry, sl, tp1, tp2, tp3, aligned, plan, time_received`, same order and formats)
  + one extra `details` object (zone hi/lo, chart stop/TP, conviction, ADX values, optional indicator values, data status). The
  existing dashboard ignores `details`; new pages can show it.
* One row per ticker of the single universe (1,228); tickers with fewer than **250 daily bars** are skipped and listed in the run report (never guessed).
* Dashboard filters preserved: hide FORMING/VOID, latest date only, Signal / MTF / Trade-plan multiselects; AVOID hidden by default (T4).
* Optional indicators (RSI, MACD, volume ratio) ship disabled; core Pine indicators cannot be disabled.

## 6. Decisions - FINAL (you delegated: "remove the ambiguity, redesign if messy")
**Observed problem (your 217 rows):** the dashboard shows 76 rows today, but only **20 (26%)** are clean "aligned long" setups;
31 are macro-downtrend (AVOID), the rest mixed / low conviction. Stop distance is 2-13% (median 6.6%), TP3 gain 6-38% (median 20%).
| # | Decision | Why |
|---|---|---|
| D1 | **FORMING = no qualified upward impulse yet.** Stopped -> VOID, target reached -> TARGET HIT; all three hidden by default. | Removes the ambiguity; visible result unchanged vs today. |
| D2 | **Bearish higher-timeframe bias -> always "AVOID: Macro Downtrend".** | Long-only, retracement-in-uptrend strategy. |
| D3 | **Skip tickers with < 250 daily bars** (listed in run report). | No guessing on short history. |
| D4 | `time_received` = IST, same format. | Local time for the user. |
| D5 | **`cross_lookback_bars` default 3** (was 1). BUY TRIGGERED = EMA9 crossed above EMA21 on any of the last 3 bars AND setup in zone **today**. | The scan runs once a day; a cross yesterday while still in the zone is still actionable. Set to 1 for the original behaviour. |
| D6 | **Grade A / B / C + one-line reason + default ranking (new, additive).** | Turns 76 mixed rows into a short ordered list. |
| D7 | **Rows with stale data (last bar > 7 days old) never get a grade.** | Never act on stale signals. |
| D8 | **"Changed today / state since" tracking** from the previous run. | Shows what is new, not what has sat there for weeks. |
| D9 | **Sideways is allowed**: setup age is displayed and used only as a tie-breaker, never as a cut-off. | Your goal: bullish *or* sideways. |

**Grade rules (explicit, all thresholds in `config/indicators.yaml`):**
* **A** = BUY TRIGGERED or IN BUY ZONE, plan = "BUY pullbacks" (weekly+monthly both up, conviction >= 20) and R:R at today's close >= 1.2.
* **B** = BUY TRIGGERED or IN BUY ZONE with plan "low conviction - wait" or "CAUTION: Low Conviction" (mixed), **or** an A-candidate with R:R < 1.2.
* **C** = WAIT FOR BUY ZONE (retracement not reached yet) with any plan except AVOID - the watch list.
* No grade: FORMING, VOID, TARGET HIT, any AVOID, stale data.
* Ranking: grade, then BUY TRIGGERED before IN ZONE before WAIT, then higher R:R, then fresher setup.
* R:R at close = (chart TP1 - close) / (close - chart stop). Inside the zone it lies between roughly 1.0 and 2.3 (deeper retracement = higher).
The thresholds (1.2, 20, 3 bars, 250 bars) are **starting values, not proven**. The Custom Analysis backtest module (later) will calibrate them on stored history.

**Dashboard (Phase 9):** tab 1 "Actionable" (A+B, ranked), tab 2 "Watch" (C), tab 3 "All signals" = your original table and filters unchanged.
New columns (additive): grade, reason, retrace %, setup age, R:R, changed today. Your 10 original columns and formats are untouched.

## 7. How parity will be proven (no network here)
* Unit tests for each building block (RMA/ATR, pivots, CHoCH, scoring, zone/latch, weekly/monthly build) on small hand-checkable series.
* `scripts/tradingview_spotcheck.py` prints, for tickers you choose, our state, zone high/low, chart stop/TP1, bias per timeframe and
  the levels. You compare with the TradingView chip/HUD on the same day. Any mismatch is a bug to fix, not a rule to change.
* Golden check: the 4 BUY TRIGGERED stocks and the 13 recent IN-ZONE stocks from your data are re-checked once price history is stored.
