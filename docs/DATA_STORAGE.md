# Data storage in the repo (Phase 4)

Everything the platform downloads or computes is saved **in your GitHub repository**, on a separate orphan branch
called `data`. Code stays on `main` (small, clean history); data lives on `data` (large, disposable history).
Locally the `data` branch is checked out into the folder `data_store/` (a git *worktree*), which `main` ignores.

## What is committed to the `data` branch
| Folder in `data_store/` | Content | Size (est.) |
|---|---|---|
| `raw/ohlcv/yahoo/<SYMBOL>.csv` + `.meta.json` | full daily history per stock, **append-only** | ~60 MB packed for 1,228 stocks x 10y; grows ~70 KB/day |
| `raw/info/yahoo/<SYMBOL>.json` | Yahoo info snapshot (market cap, PE, sector...) refreshed daily | ~5 MB |
| `raw/statements/yahoo/<SYMBOL>.json` | income/balance sheet, refreshed weekly | ~10 MB |
| `ledger/downloads_YYYY-MM.csv`, `ledger/runs/*.json` | every provider call + per-run counters (proof of no duplicate downloads) | small |
| `state/` | job state, annual-report queue (Phases 8, 10) | small |
| `results/`, `published/` | final screener outputs and the versioned JSON the apps read (Phases 5-10) | small |
Not committed: lock files, temp files, raw XBRL/PDF downloads (only the extracted values + evidence are kept - the
filings can be re-downloaded and would add gigabytes).

The size estimate comes from a synthetic 10-year series (about 56 bytes per day per stock). Real data compresses
similarly; the `Data Management` page will show the live size from `DataManager.stats()["store"]`.

## One-time setup (PowerShell, in the project folder)
```powershell
git checkout --orphan data
git rm -rf .
git commit --allow-empty -m "init data branch"
git push origin data
git checkout main
git worktree add data_store data        # creates D:\Dev\Python\Projects\mizan-market-intelligence\data_store
```
After this, code that writes to `data_store/` is writing into the `data` branch. To publish:
```powershell
cd data_store; git add -A; git commit -m "daily data"; git push; cd ..
```
(GitHub Actions does this automatically from Phase 10.)

## GitHub Actions (preview of Phase 10)
```yaml
permissions: { contents: write }
concurrency: { group: mizan-data, cancel-in-progress: false }     # two runs never write the data branch together
steps:
  - uses: actions/checkout@v4                                      # main -> code
  - uses: actions/checkout@v4
    with: { ref: data, path: data_store }                          # data branch -> data_store/
  - run: python -m mizan.execution.run_daily                       # (Phase 10)
  - run: |
      cd data_store && git config user.name mizan-bot && git config user.email bot@users.noreply.github.com
      git add -A && (git diff --cached --quiet || git commit -m "data $(date -u +%F)") && git pull --rebase && git push
```
## Keeping the branch small
* Daily commits only add one CSV line per stock (git stores a small delta).
* If `data` ever exceeds ~700 MB, squash its history (the history of data files has little value):
  `git checkout --orphan data-new && git add -A && git commit -m "squash" && git branch -M data && git push -f origin data`
* Never commit raw filings, PDFs or model caches.

## Design rules enforced by tests
* One growing dataset per ticker; requests for other windows reuse it and fetch only the missing part.
* Cache key = `type | source | ticker | period | schema-version`; bump the version to invalidate on purpose.
* A dividend / split / bonus revises history at Yahoo; the 7-day overlap on each incremental fetch detects it and
  refreshes that ticker's history (otherwise stored adjusted prices would silently drift).
* Screeners get **copies**; reading never writes; writes are atomic (temp file + replace).
* Yahoo rate limit -> back-off -> after 3 consecutive failures the circuit breaker stops the run; the next run resumes.
