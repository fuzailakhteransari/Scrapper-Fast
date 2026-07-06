param(
    [Parameter(Mandatory = $true)]
    [string]$InputCsv,

    [string]$WebsiteColumn = "",
    [int]$Concurrency = 40,
    [int]$MaxPages = 6,
    [switch]$GoogleSheets,
    [switch]$RetryFailed
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $Root ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Virtual environment not found. Run .\setup.ps1 first."
}

$Arguments = @(
    "-m", "contact_scraper",
    $InputCsv,
    "--output-dir", (Join-Path $Root "output"),
    "--concurrency", $Concurrency,
    "--max-pages", $MaxPages
)

if ($WebsiteColumn) {
    $Arguments += @("--website-column", $WebsiteColumn)
}
if ($GoogleSheets) {
    $Arguments += "--google-sheets"
}
if ($RetryFailed) {
    $Arguments += "--retry-failed"
}

& $Python @Arguments
exit $LASTEXITCODE

