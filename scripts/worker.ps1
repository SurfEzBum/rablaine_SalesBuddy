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

# Share the app's isolated Azure CLI context (matches scripts/server.ps1) so MSX
# token acquisition uses the same file-based cache the web server uses. Respects
# an already-set AZURE_CONFIG_DIR (e.g. when launched by the supervisor).
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

$env:SALESBUDDY_ROLE = 'worker'
python -m app.worker
