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

python -m app.supervisor
