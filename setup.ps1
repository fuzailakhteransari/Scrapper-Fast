$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Venv = Join-Path $Root ".venv"
$Python = Join-Path $Venv "Scripts\python.exe"

if (-not (Test-Path -LiteralPath $Python)) {
    python -m venv $Venv
}

& $Python -m pip install --upgrade pip
& $Python -m pip install -e "$Root[browser,google,dev]"

Write-Host ""
Write-Host "Setup complete."
Write-Host "Run: .\.venv\Scripts\python.exe -m contact_scraper input.csv"
Write-Host "UI:  Double-click 'Launch Scraper UI.cmd'"
