# better-memory Windows bootstrap: uv -> deps -> wiring. Idempotent.
# Runnable from anywhere: cds to the repo root itself (lift-pass fix — the
# first draft assumed the caller's cwd was the repo root).
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "[setup] uv not found - installing..."
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget) {
        winget install --id=astral-sh.uv -e --accept-source-agreements --accept-package-agreements
    } else {
        powershell -ExecutionPolicy ByPass -NoProfile -Command `
            "irm https://astral.sh/uv/install.ps1 | iex"
    }
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "User") + ";" + `
                [Environment]::GetEnvironmentVariable("Path", "Machine")
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Write-Error "[setup] uv installed but not on PATH - open a new shell and re-run."
        exit 1
    }
}

Write-Host "[setup] Syncing dependencies..."
uv sync
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "[setup] Installing Claude Code wiring..."
uv run better-memory setup
exit $LASTEXITCODE
