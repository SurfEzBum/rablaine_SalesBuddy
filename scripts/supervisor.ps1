# Sales Buddy - Supervisor (dev)
#
# Runs the whole stack under one watchdog: spawns the web server AND the
# background worker, and restarts either one if it crashes or hangs. This is the
# local stand-in for what an Electron/Tauri main process will eventually do.
#
# Dev usage: stop any separate `flask run` / `worker.ps1` first, then:
#
#   .\scripts\supervisor.ps1
#
# The supervisor spawns waitress on $PORT (from .env, default 5151) plus the
# worker, and logs supervisor/child events to the lifecycle log (role=supervisor).

$RepoRoot = Split-Path $PSScriptRoot -Parent
Set-Location $RepoRoot

$Activate = Join-Path $RepoRoot 'venv\Scripts\Activate.ps1'
if (Test-Path $Activate) {
    & $Activate
}

# Share the app's isolated Azure CLI context (matches scripts/server.ps1) so both
# the web and worker children the supervisor spawns use the same file-based MSX
# token cache. Respects an already-set AZURE_CONFIG_DIR (e.g. from server.ps1).
if (-not $env:AZURE_CONFIG_DIR) {
    $flaskEnv = 'production'
    $envFile = Join-Path $RepoRoot '.env'
    if (Test-Path $envFile) {
        foreach ($line in Get-Content $envFile) {
            if ($line -match '^\s*FLASK_ENV\s*=\s*(.+?)\s*$') {
                $flaskEnv = $Matches[1].Trim().Trim('"').Trim("'"); break
            }
        }
    }
    $profileFolder = if ($flaskEnv -match '^(?i)development$') { 'SalesBuddyDev' } else { 'SalesBuddy' }
    $sbHome = Join-Path $env:USERPROFILE $profileFolder
    $env:SALESBUDDY_HOME = $sbHome
    $env:AZURE_CONFIG_DIR = Join-Path $sbHome '.azure'
}

python -m app.supervisor
