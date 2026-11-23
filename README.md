# Mizan Market Intelligence

Three independent screeners (Swing Setup Radar · Quality Value Rebound · Shariah Compliance Screener),
one stock universe, one data layer, one dashboard. *Mizan* = "the balance" - weighing every stock through three lenses.

Status: **Phase 11 go-live kit ready** (scheduler, GitHub workflows, published JSON, hosted dashboard). Follow docs/GO_LIVE.md.

## Setup on Windows + PyCharm (project on D:)
1. Unzip so the folder is `D:\Dev\Python\Projects\mizan-market-intelligence`.
2. PyCharm -> File -> Open -> that folder.
3. Settings -> Project -> Python Interpreter -> Add Interpreter -> Add Local Interpreter -> Virtualenv Environment -> **New**
   - Location: `D:\Dev\Python\Projects\mizan-market-intelligence\.venv`
   - Base interpreter: your D-drive `python.exe` (3.11 or 3.12)
4. Terminal (PyCharm, bottom bar):
   ```
   pip install -r requirements-dev.txt
   pip install -e .
   ```
5. Right-click `src` -> Mark Directory as -> **Sources Root**; `tests` -> **Test Sources Root**;
   `data\raw`, `data\normalized`, `data\state` -> **Excluded**.
6. Copy `.env.example` to `.env` and fill in secrets (never commit `.env`).
7. Check: `python -m unittest discover -s tests -v`  -> 20 tests OK.

## Where things live
| Folder | Purpose |
|---|---|
| `data/universe/stock_universe.txt` | the ONE stock list - edit this file |
| `config/` | app.yaml (limits, paths), modules.yaml (register screeners), indicators.yaml (technical indicators) |
| `src/mizan/` | all application code (see each package's docstring) |
| `golden_master/` | frozen original outputs + test fixtures (read-only) |
| `original/` | untouched copies of the source screeners (reference) |
| `data_store/` | checkout of the `data` branch: cache, ledger, results (see docs/DATA_STORAGE.md) |
| `docs/BUSINESS_RULES.md` | every rule + change register |
