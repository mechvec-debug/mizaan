# Stock Universe (Phase 3)

**One file, one service.** `data/universe/stock_universe.txt` is read only by `mizan.universe.UniverseService`.
Every screener, the annual-report queue and any future module receives an immutable `UniverseSnapshot` from it.

## File format
```
TCS                  one NSE symbol per line (no .NS needed; M&M, BAJAJ-AUTO are fine)
infy   # Infosys     case-insensitive; a trailing "# note" is allowed
# WIPRO              a comment containing ONE word = a DISABLED stock (kept, but not screened)
# any other comment  ignored
```
Normalisation: upper-case, trim, `NSE:`/`BSE:` prefix and `.NS`/`.BO` suffix removed. Valid = `[A-Z0-9][A-Z0-9&-]{0,19}`.
The text you typed is always kept (`Entry.raw`).

## Guarantees
* Empty universe -> `EmptyUniverseError` (modules refuse to run; never "success with zero stocks").
* Invalid lines are excluded **and** reported (log warning + `snapshot.invalid`); strict mode raises.
* Duplicates are collapsed and reported with the first line number.
* `snapshot.version` (12 hex chars) changes only when the active set changes; it goes into cache keys and published manifests.
* The service re-reads the file automatically when it changes (size/mtime) - no restart needed.
* Admin edits are atomic (temp file + replace), keep the last 20 backups in `data/universe/backups/`, preserve comments,
  and return a `ChangeReport` listing added / already present / exists-but-disabled / invalid / not found.

## Command line (PyCharm terminal)
```
python -m mizan.universe check
python -m mizan.universe add TCS INFY
python -m mizan.universe import my_list.csv      # .txt, or .csv with a header: symbol / ticker / code
python -m mizan.universe disable INFY
python -m mizan.universe enable INFY
python -m mizan.universe remove INFY
```
Bulk CSV without a recognisable header is refused (we never guess the ticker column).

## Provider symbols
`to_yahoo("TCS") -> "TCS.NS"` (same as the original Fundamental screener). BSE scrip codes come from the ISIN mapping built in Phase 4/8.
