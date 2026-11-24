# Dev runner for Windows PowerShell.
#   .\scripts\dev.ps1          start API (serves the last built frontend)
#   .\scripts\dev.ps1 -Build   build the frontend first, then start API
#   .\scripts\dev.ps1 -Web     also start the Vite dev server (hot reload) in a new window
param(
    [switch]$Build,
    [switch]$Web,
    [int]$Port = 8000
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'

if (-not (Test-Path $python)) {
    throw "venv not found. Create it first: python -m venv .venv; .\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt"
}

# pip/npm need a writable temp dir inside the project when running sandboxed.
$env:TMP = Join-Path $root '.tmp'
$env:TEMP = $env:TMP
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
    Write-Host "Vite: http://127.0.0.1:5173" -ForegroundColor Cyan
}

Write-Host "API:  http://127.0.0.1:$Port" -ForegroundColor Cyan
Write-Host "Docs: http://127.0.0.1:$Port/docs" -ForegroundColor DarkGray

Push-Location (Join-Path $root 'backend')
try {
    & $python -m uvicorn app.main:app --host 127.0.0.1 --port $Port --reload
} finally {
    Pop-Location
}
