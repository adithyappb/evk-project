# Safe git commit on OneDrive-synced Windows paths.
# OneDrive locks .git/objects during auto-gc, causing hundreds of
# "Deletion of directory '.git/objects/XX' failed" prompts.
#
# Usage:  .\scripts\git-commit.ps1 -m "your message"
#         .\scripts\git-commit.ps1 -m "your message" --no-verify

param(
    [Parameter(Mandatory = $true)]
    [string]$m,
    [switch]$NoVerify
)

$ErrorActionPreference = "Stop"
Set-Location (Resolve-Path (Join-Path $PSScriptRoot ".."))

$args = @(
    "-c", "gc.auto=0",
    "-c", "maintenance.auto=false",
    "commit", "-m", $m
)
if ($NoVerify) { $args += "--no-verify" }

& git @args
exit $LASTEXITCODE
