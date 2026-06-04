param(
    [string]$Host = "127.0.0.1",
    [int]$ApiPort = 8000,
    [int]$WebPort = 5173
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot

$safeRoot = $repoRoot.Replace("'", "''")
$backendCmd = "Set-Location -LiteralPath '$safeRoot'; python -m uvicorn main:app --reload --host $Host --port $ApiPort"
$frontendCmd = "Set-Location -LiteralPath '$safeRoot'; npm run dev -- --host $Host --port $WebPort"

Write-Host "Starting backend on http://$Host:$ApiPort ..."
Start-Process powershell -ArgumentList "-NoExit", "-Command", $backendCmd | Out-Null

Write-Host "Starting frontend on http://$Host:$WebPort ..."
Start-Process powershell -ArgumentList "-NoExit", "-Command", $frontendCmd | Out-Null

Write-Host ""
Write-Host "Both services were launched in separate PowerShell windows."
Write-Host "Backend health: http://$Host:$ApiPort/health/"
Write-Host "Frontend UI:   http://$Host:$WebPort/"
