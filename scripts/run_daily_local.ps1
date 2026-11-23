# Run the daily pipeline on THIS PC and push the results to the data branch.
# Use it when GitHub's shared servers get rate-limited by Yahoo, or as the scheduled job in Windows Task Scheduler.
# One-time: set the repo URL, e.g.  setx MIZAN_DATA_REPO_URL "https://github.com/OWNER/REPO.git"   (then open a new PowerShell window)
param([string]$RepoUrl = $env:MIZAN_DATA_REPO_URL, [int]$BudgetMinutes = 300)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
if (-not $RepoUrl) { throw "Set MIZAN_DATA_REPO_URL or pass -RepoUrl" }
if (-not (Test-Path "data_store\.git")) {
    if ((Test-Path "data_store") -and (Get-ChildItem "data_store" -Force | Select-Object -First 1)) { throw "data_store exists and is not a git checkout; move it away first" }
    git clone --branch data --single-branch $RepoUrl data_store
}
git -C data_store pull --rebase origin data
python -m mizan.execution run --trigger local --time-budget-min $BudgetMinutes --publish
$code = $LASTEXITCODE
"tmp/`nlocks/" | Set-Content -Path "data_store\.gitignore" -Encoding ascii
git -C data_store add -A
git -C data_store diff --cached --quiet
if ($LASTEXITCODE -ne 0) {
    git -C data_store commit -m "data $(Get-Date -Format yyyy-MM-dd) (local run)"
    git -C data_store push origin HEAD:data
}
exit $code
