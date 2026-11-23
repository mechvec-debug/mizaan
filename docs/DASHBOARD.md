# Dashboard, module registry, login (Phase 9)

## Start it
```
pip install -r requirements-dev.txt
pip install -e .
streamlit run src/mizan/dashboard/app.py
```
Ten pages, in this order: **Executive Overview - Screener 1 (Swing Setup Radar) - Screener 2 (Quality Value Rebound) - Screener 3
(Shariah Compliance) - Annual Report Intelligence - Data Management - Execution Monitor - Custom Screeners - Custom Analysis -
System Configuration**. Enabled custom modules appear under their section automatically.

## Test it on 5-6 stocks first (separate data, real list untouched)
1. Copy `config/app.local.yaml.example` to `config/app.local.yaml` (git-ignored). It points the app at `data/universe/test_universe.txt`
   (TCS, INFY, RELIANCE, HINDUNILVR, SUNPHARMA, ASIANPAINT) and a separate `data_store_test/` folder. Four of the six have a seed BSE
   code, so the XBRL path is exercised too.
2. `python scripts/run_test_batch.py` runs Technical, Fundamental and Shariah once on live data and prints what happened per module.
   (`--annual` adds the annual-report batch once its two limits are set; until then it reports "not_configured", it does not fail.)
3. `streamlit run src/mizan/dashboard/app.py` - the sidebar shows "TEST universe in use".
4. To go back to the full list: delete `config/app.local.yaml`.
The annual-report limits stay open as agreed; that page shows a clear "Not configured yet" notice.

## Who can do what
* **Everyone (no login): read-only** - every results page.
* **Admin (Sami, mechvec@gmail.com): after login** - run modules, add / remove / disable / enable stocks (type, paste, or upload a txt/csv - for new
  IPOs), record Shariah evidence (with source + justification), enable / disable modules.
* The password is **never in the code or the repo** - only its salted hash, kept in the project's git-ignored `.env` file.
  **Turn the admin login on (once):**
  1. `python -m mizan.core.auth hash --write-env`   (asks for the password twice, at least 10 characters; writes `MIZAN_ADMIN_PASSWORD_HASH=...` into `.env`)
  2. restart the dashboard (Ctrl+C, then `python -m streamlit run src/mizan/dashboard/app.py`)
  3. sidebar -> **🔐 Admin login** -> e-mail `mechvec@gmail.com` + your password -> Sign in. The sidebar then shows "Signed in as Sami (admin)".
  Until the hash exists the sidebar says "Admin login is not configured" and everyone is a read-only viewer (it fails closed).
  Five wrong attempts lock the login for 15 minutes. `python scripts/doctor.py` tells you whether the login is set up.

## How to run things
**From the dashboard (admin only).** After signing in, every module page shows a blue **🚀 Run ...** button at the top
(Swing Setup Radar, Quality Value Rebound, Shariah Compliance, Annual Report batch), and **Execution Monitor** has
**▶️ Run all enabled modules (one after another)** plus the last status of each module and the run history. The page waits until the run finishes.
* On the 6-stock test universe a run takes a few minutes. On the full 1,228-stock list the first run takes hours - do not start that from the browser.
* A viewer sees the same pages without the buttons ("🔒 Only the admin can run modules").
**From a terminal (any size, survives closing the browser).**
* `python scripts/run_test_batch.py` - Technical + Fundamental + Shariah on the test universe, with the reason for every failed / skipped stock.
* `python scripts/run_test_batch.py --universe data/universe/stock_universe.txt --store data_store` - the full list (hours; progress is cached, so an interrupted run continues).
* one module: `python -m mizan.screeners.technical` / `.fundamental` / `.shariah` / `python -m mizan.annual_report run`.
**Unattended, every day:** the scheduled GitHub run is Phase 10.

## Screener pages keep their original designs
* **Technical**: original title, Quick Filters (Signal / MTF Alignment / Trade Plan), red "Total scan" line and table, now inside the
  third tab; the first two tabs show the graded Actionable (A/B) and Watch (C) lists with the notification price.
* **Fundamental**: the original two-column sections. The page title is the new name (Quality Value Rebound) and the caption now states the
  real rule values (35-65 % below the high; watch from 25 %) - the old text said 50-70 % / 40 %, which never matched the code.
* **Shariah**: the original dark theme, KPI cards (plus an **Undetermined** card), one expander per stock with six rule cards (PASS / FAIL /
  **PENDING** with the reason). The old sidebar watch-list editor moved to System Configuration (one stock list for everything).
The Google-Sheet-based Fundamental app and the Shariah "Run" button / PDF uploader are replaced by the admin run buttons and the evidence form.

## Adding a module (no engine is edited)
1. Write `run(ctx)` in `src/mizan/screeners/custom/` (see `near_52w_high.py`): read data through `ctx.data_manager()`, write rows to
   `ctx.results_dir("<id>")`, return a small dict.
2. Add one entry to `config/modules.yaml` (id, display_name, category `custom_screener` or `custom_analysis`, `entrypoint`, `results_file`,
   `required_columns`, `enabled`). A bad declaration is refused with the reasons (shown on the Custom pages), it never breaks the app.
3. It shows in the navigation and gets a generic results page unless it declares its own `page` function.
A crash inside a module or its page is isolated: that module reports an error, everything else keeps working.

## Not verified in the build environment
Streamlit is not installed there, so the pages were exercised through a recording stand-in (every page, as viewer and admin, plus all
admin actions) but **never rendered in a real browser**. Layout, widths and styling need your eyes on the first run.
Edits to the stock list change the file on the machine running the dashboard; for the GitHub-hosted setup commit the file (Phase 10 adds
the automatic commit step).

## Troubleshooting
**`streamlit` fails with an ImportError that mentions another folder (e.g. a different project's `.venv`)**
The prompt shows `(.venv)`, but the `streamlit` *command* can resolve to another environment's copy. Always start the dashboard through the
active Python:
```
python -m streamlit run src/mizan/dashboard/app.py
```
Check what is installed in THIS environment:  `python -m pip show streamlit starlette`.  If streamlit is missing: `python -m pip install -r requirements-dev.txt`.
If it is installed but the import fails (streamlit and starlette versions out of step): `python -m pip install -U streamlit starlette`, then `python -m pip check`.
`python scripts/doctor.py` checks all of this (and the delta files) in one go and prints what to fix.

**A stock shows "failed" in a run summary** - the one-line summary only has counts. `python scripts/run_test_batch.py` now prints each failed / skipped stock
with its reason; the same reasons are in `data_store*/results/<module>/run_report.json` (`failed_detail`, `data_notes`) and in `python scripts/doctor.py` section 6.

**The test suite is slow on Windows** (it creates thousands of small files): exclude the project folder from real-time antivirus scanning, or find the slow tests with
`python -m pytest tests -q --durations=15`.
