# EVK one-shot bootstrap for Windows / PowerShell.
#
# Why this exists: when the project lives under OneDrive, `.venv` inside the
# project folder fights with OneDrive's cloud-file provider (error 0x80070005
# / ERROR_CLOUD_FILE_INCOMPATIBLE_HARDLINKS). We pin the venv to
# %LOCALAPPDATA% instead, which OneDrive doesn't touch.
#
# Usage (from project root):
#   . .\scripts\setup.ps1          # dot-source so env vars persist in the shell
#   uv run evk seed
#   uv run evk serve

$ErrorActionPreference = "Stop"

$projectName = Split-Path -Leaf (Get-Location)
$venvRoot = Join-Path $env:LOCALAPPDATA "uv-venvs\$projectName"

$env:UV_PROJECT_ENVIRONMENT = $venvRoot
$env:UV_LINK_MODE = "copy"

Write-Host "UV_PROJECT_ENVIRONMENT = $venvRoot"
Write-Host "UV_LINK_MODE           = copy"

uv sync
if ($LASTEXITCODE -ne 0) { throw "uv sync failed" }

if (-not (Test-Path .env)) {
    Copy-Item .env.example .env
    Write-Host ".env created from .env.example"
}

Write-Host ""
Write-Host "Ready. Try:"
Write-Host "  uv run evk info"
Write-Host "  uv run evk seed"
Write-Host "  uv run evk serve     # http://localhost:8080"
