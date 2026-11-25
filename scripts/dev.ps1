# Dev runner for Windows PowerShell. Run it from anywhere:
#   .\scripts\dev.ps1                 start the API on the last frontend build
#   .\scripts\dev.ps1 -Build          rebuild the frontend, then start the API
#   .\scripts\dev.ps1 -Web            also start Vite with hot reload (new window)
#   .\scripts\dev.ps1 -Port 8010      another port
#   .\scripts\dev.ps1 -NoReload       do not restart on Python changes
#
# Stop it with Ctrl+C. Nothing starts by itself after a reboot: the API is an
# ordinary process, so it has to be started again (see README, "Быстрый старт").
param(
    [switch]$Build,
    [switch]$Web,
    [switch]$NoReload,
    [int]$Port = 8000
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'

if (-not (Test-Path $python)) {
    throw "venv not found. Create it first: python -m venv .venv; .\.venv\Scripts\python.exe -m pip install -r backend\requirements-dev.txt"
}

# pip/npm need a writable temp dir inside the project when running sandboxed.
$env:TMP = Join-Path $root '.tmp'
$env:TEMP = $env:TMP
$env:HF_HOME = Join-Path $root '.models'
New-Item -ItemType Directory -Force -Path $env:TMP | Out-Null

if ($Build) {
    Push-Location (Join-Path $root 'frontend')
    try { npm run build } finally { Pop-Location }
}

if ($Web) {
    Start-Process pwsh -ArgumentList @(
        '-NoExit', '-Command',
        "Set-Location '$root\frontend'; `$env:VITE_BACKEND='http://127.0.0.1:$Port'; npm run dev"
    )
    Write-Host "Vite: http://127.0.0.1:5173 (hot reload)" -ForegroundColor Cyan
}

$reload = @()
if (-not $NoReload) {
    & $python -c "import watchfiles" 2>$null
    if ($LASTEXITCODE -eq 0) {
        $reload = @('--reload')
    } else {
        Write-Host "watchfiles is not installed: starting without auto-reload." -ForegroundColor Yellow
        Write-Host "  .\.venv\Scripts\python.exe -m pip install -r backend\requirements-dev.txt" -ForegroundColor DarkGray
    }
}

$base = "http://127.0.0.1:$Port"
if (-not (Test-Path (Join-Path $root 'frontend\dist\index.html'))) {
    Write-Host "Frontend is not built yet: the API will serve a placeholder page." -ForegroundColor Yellow
    Write-Host "  .\scripts\dev.ps1 -Build" -ForegroundColor DarkGray
}

Write-Host ""
Write-Host "  UI:   $base" -ForegroundColor Cyan
Write-Host "  Docs: $base/docs" -ForegroundColor DarkGray
Write-Host "  Stop: Ctrl+C" -ForegroundColor DarkGray
Write-Host ""

Push-Location (Join-Path $root 'backend')
try {
    & $python -m uvicorn app.main:app --host 127.0.0.1 --port $Port @reload
} finally {
    Pop-Location
}
