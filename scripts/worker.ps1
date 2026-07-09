# Sales Buddy - Background worker (dev)
#
# Runs the heavy background schedulers (MSX / WorkIQ / meeting aura) and the
# durable job-queue consumer in a process separate from the web server, so a
# slow or hung background job can never wedge the UI.
#
# Dev usage: run the web server in one terminal (`flask run`) and this worker
# in another:
#
#   .\scripts\worker.ps1
#
# The worker tags its lifecycle events with role='worker' and keeps a heartbeat
# that the web /health endpoint reports.

$RepoRoot = Split-Path $PSScriptRoot -Parent
Set-Location $RepoRoot

$Activate = Join-Path $RepoRoot 'venv\Scripts\Activate.ps1'
if (Test-Path $Activate) {
    & $Activate
}

$env:SALESBUDDY_ROLE = 'worker'
python -m app.worker
