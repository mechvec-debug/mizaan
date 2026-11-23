# Go live (Phase 11)

Everything below is in the order to do it. Commands are for Windows PowerShell in the project folder. Allow about 45 minutes for steps 1-6.

## 0. Three decisions that change the setup
| Decision | What to know |
|---|---|
| **Repository visibility** | A **public** repo has unlimited free GitHub Actions minutes and works with the simplest hosting. A **private** repo gets 2,000 free Actions minutes a month. One daily run of the full 1,228 stocks needs about 120 minutes (22 trading days = ~2,640 min), and the first full load about 4 hours - so a private repo needs a paid plan (or the local runner, step 9). Public means the code **and the screening results** are public; secrets stay private either way. |
| **Will Yahoo accept GitHub's servers?** | Not guaranteed. Yahoo limits requests per IP and GitHub's shared addresses are sometimes blocked. The pipeline copes (it pauses downloads, saves what it finished, continues next run) but if most days end with "rate limited" in the run summary, use the local runner (step 9). You will know after the first two runs. |
| **Which stocks** | `data/universe/stock_universe.txt` is the live list (1,228). Edit and commit it any time (IPOs: add the symbol, push). |

## 1. Check the repository before the first push
```
python scripts/prepush_check.py
```
It scans exactly what git would upload (it respects `.gitignore`). It must print **"nothing sensitive found"**. It flags API keys, private keys, password hashes, a tracked `.env`, tracked `data_store` folders and files over 20 MB. (`.env`, `config/app.local.yaml`, `data_store/` and `data_store_test/` are already ignored.)

## 2. Push
```
git add -A
git commit -m "Mizan: first release"
git remote add origin https://github.com/<you>/<repo>.git
git push -u origin master
```
Your default branch is `master`; scheduled workflows run from the default branch, so nothing else is needed. Data is saved on a separate branch called `data` that the first run creates.

## 3. GitHub settings (once, in the browser)
1. **Settings -> Actions -> General -> Workflow permissions** -> *Read and write permissions* -> Save. (The workflows write the `data` branch.)
2. **Actions** tab -> if asked, *I understand my workflows, enable them*.
3. Later, when you have the AI limits: **Settings -> Secrets and variables -> Actions -> New repository secret** -> `GEMINI_API_KEY`. (Not needed for the first runs.)

## 4. First run - a short, safe test
**Actions -> Daily pipeline -> Run workflow** -> leave *modules* empty, set **time_budget_minutes = 20** -> Run.
It checks out (and creates) the `data` branch, runs every module on the real list for 20 minutes, validates and publishes, then **saves everything to the `data` branch** and stops cleanly. Open the finished run: the **summary** shows each module, its status and alerts.
Expected: Technical / Fundamental / Shariah *ok* or *partial* (a first run has not reached every stock yet), annual report *not configured* (a warning, not a failure). Then check the repository has a branch `data` containing `results/`, `raw/`, `ledger/`, `state/` and `published/v1/manifest.json`.

## 5. Let it fill in
The schedule is **Monday-Friday 10:45 UTC (16:15 IST)**. Every run continues where the last stopped (prices, statements and queues are cached), so the first full load completes over about 2-3 runs. You can speed it up: *Run workflow* with *resume* ticked, or a bigger budget (max 330). After that each day is a ~2 hour top-up.
A red run means a module **failed** (the summary says which and why); data is still saved. Running out of time or being rate-limited is **not** red - it is reported as a warning and continues next run.

## 6. Hosted dashboard (Streamlit Community Cloud)
1. share.streamlit.io -> **Create app** -> repository, branch `master`, main file **`src/mizan/dashboard/app.py`**, Python 3.12.
2. **Advanced settings -> Secrets** (paste, then edit the placeholders):
```
MIZAN_DATA_REPO_URL = "https://github.com/<you>/<repo>.git"
```
   For a **private** repo use a fine-grained token with *Contents: read-only* on this repo: `"https://x-access-token:<TOKEN>@github.com/<you>/<repo>.git"`.
3. Deploy. The app pulls the `data` branch (read-only copy, refreshed every 15 minutes; the sidebar shows "Data from the repository: ...").
**Do not** add `MIZAN_ADMIN_PASSWORD_HASH` to the hosted app: without it everyone is a read-only viewer, which is what a public dashboard should be. Run, edit-the-universe and evidence tools stay on your own PC (`python -m streamlit run src/mizan/dashboard/app.py` with the admin login).

## 7. Feeds for other apps (Cloudflare Pages / Android / anything)
The same run publishes **`published/v1/`** on the `data` branch: `manifest.json` (every file with SHA-256, size, rows, as-of date), `technical.json`, `fundamental.json`, `shariah.json`, `annual_report.json`, `universe.json`, `status.json`. A reader fetches the manifest first, compares `generated_at` / hashes, then downloads what changed. Rows are one per line; every file has `schema: 1`, `module`, `as_of`, `count`, `rows`.
**Cloudflare Pages:** Workers & Pages -> Create -> Pages -> connect the repo -> **Production branch: `data`**, build command empty, **output directory `published`** -> Deploy. Feed: `https://<project>.pages.dev/v1/manifest.json` (CORS is already enabled by the `_headers` file). For a *public* repo you can also read `https://raw.githubusercontent.com/<you>/<repo>/data/published/v1/manifest.json` with no hosting at all.

## 8. Daily life
| You want to | Do this |
|---|---|
| See what happened | Actions -> latest run -> summary; or the dashboard's **Execution Monitor** (last run, alerts, next run, published-data health) |
| Run now / continue / retry | Actions -> Run workflow (tick *resume* to continue what was left; set *modules* e.g. `shariah`); or Execution Monitor buttons on your PC |
| Add a new listing | add the symbol to `data/universe/stock_universe.txt`, commit, push (or use System Configuration on your PC, then commit) |
| Start the annual-report AI | set `annual_report.daily_token_budget` and `requests_per_minute` in `config/app.yaml`, commit, add the `GEMINI_API_KEY` secret. The *Annual report batch* workflow (12:30 UTC daily) does the rest, at most 100 stocks a day |
| Pause everything | Actions -> each workflow -> **...** -> *Disable workflow* |
| Shrink the `data` branch | Actions -> *Data branch maintenance* -> type `SQUASH` (history only; current data is untouched) |

## 9. Fallback: run the pipeline on your own PC
Use this if Yahoo blocks GitHub, or you stay on a private free plan. Keep only **one** writer: comment out the `schedule:` lines in `.github/workflows/daily_pipeline.yml` (keep *Run workflow*), commit and push.
1. One time: `python -m mizan.execution run --time-budget-min 330 --publish` works as it does on GitHub; `scripts\run_daily_local.ps1` adds the data-branch clone and push around it (untested in the build environment).
2. Windows **Task Scheduler** -> Create Basic Task -> Daily 16:30 -> Start a program: `powershell.exe` -> Arguments `-ExecutionPolicy Bypass -File "D:\Dev\Python\Projects\mizan-market-intelligence\scripts\run_daily_local.ps1"` -> Start in: the project folder. Your PC must be on and the virtual environment's `python` first on PATH.
Home IPs are usually accepted by Yahoo, and this costs no Actions minutes.

## 10. What is (and is not) in this release
* Included: scheduled daily pipeline, annual-report batch workflow, data saved to the `data` branch, resumable runs with a time budget, retries, run lock, status + alerts, validated published JSON, hosted read-only dashboard, pre-push scan, local runner.
* **Deferred, not needed to go live:** running modules in parallel, a Firestore mirror for an Android app (the published JSON covers it), e-mail/Telegram notifications (the run summary and a red run are the alert today), the two annual-report limits (open by agreement).
* GitHub quirks: a schedule can start several minutes late; in a *public* repo GitHub disables scheduled workflows after 60 days without any repository activity (a commit or manual run re-enables them).

## Done when
- [ ] `prepush_check.py` says nothing sensitive found
- [ ] code is on GitHub (`master`) and Actions are enabled
- [ ] a manual run finished and created the `data` branch with `published/v1/manifest.json`
- [ ] the hosted dashboard opens and its sidebar says "Data from the repository: cloned/updated"
- [ ] the next scheduled run (16:15 IST) appears in the Actions tab
