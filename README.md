# Mizan Market Intelligence

Three independent screeners (Swing Setup Radar · Quality Value Rebound · Shariah Compliance Screener),
one stock universe, one data layer, one dashboard. *Mizan* = "the balance" - weighing every stock through three lenses.
Terminal (PyCharm, bottom bar):
   ```
   pip install -r requirements-dev.txt
   pip install -e .
   ```

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
