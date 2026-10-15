param(
    [Alias("Host")][ValidatePattern('^[A-Za-z0-9.:\[\]-]+$')][string]$BindHost = "127.0.0.1",
    [int]$ApiPort = 8000,
    [int]$WebPort = 5173,
    [string]$Python = "python",
    [switch]$Dev,
    [switch]$DryRun
)

# Normal start: no --reload (a reload restarts the backend on every file change and loses running jobs).
# --timeout-graceful-shutdown 3: one Ctrl+C reaches the shutdown cleanup after at most 3 s, even while a suite job runs.
# -Dev adds --reload for development. -DryRun prints the two commands and exits.
$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot

$safeRoot = $repoRoot.Replace("'", "''")
$safePython = $Python.Replace("'", "''")
$reload = if ($Dev) { " --reload" } else { "" }
$backendCmd = "Set-Location -LiteralPath '$safeRoot'; & '$safePython' -m uvicorn main:app$reload --host $BindHost --port $ApiPort --timeout-graceful-shutdown 3"
$frontendCmd = "Set-Location -LiteralPath '$safeRoot'; npm run dev -- --host $BindHost --port $WebPort"
if ($DryRun) {
    Write-Output "backend: $backendCmd"
    Write-Output "frontend: $frontendCmd"
    exit 0
}
if ($Dev) { Write-Host "Dev mode: --reload restarts the backend on every file change and cancels running jobs." }

Write-Host "Starting backend on http://${BindHost}:${ApiPort} ..."
Start-Process powershell -ArgumentList "-NoExit", "-Command", $backendCmd | Out-Null

Write-Host "Starting frontend on http://${BindHost}:${WebPort} ..."
Start-Process powershell -ArgumentList "-NoExit", "-Command", $frontendCmd | Out-Null

Write-Host ""
Write-Host "Both services were launched in separate PowerShell windows."
Write-Host "Backend health: http://${BindHost}:${ApiPort}/health/"
Write-Host "Frontend UI:   http://${BindHost}:${WebPort}/"
