# Resolve-DbPath.ps1
#
# Single source of truth (PowerShell side) for where salesbuddy.db lives, so the
# backup / restore / server / uninstall scripts agree with the Flask app and the
# C# installer. In production the DB lives OUTSIDE the install dir (a sibling of
# it) so no install/upgrade/uninstall can delete user data.
#
# Resolution:
#   1. Read <RepoRoot>\data-path.txt (published by the app/supervisor at boot).
#   2. Fall back to the same derivation the app uses:
#        FLASK_ENV=production -> %LOCALAPPDATA%\SalesBuddy-data\salesbuddy.db
#        otherwise            -> <RepoRoot>\data\salesbuddy.db
#
# Dot-source this file, then call Resolve-SalesBuddyDbPath -RepoRoot <root>.

function Resolve-SalesBuddyDbPath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$RepoRoot
    )

    # 1. Prefer the path the app published - the authoritative source.
    $pathFile = Join-Path $RepoRoot 'data-path.txt'
    if (Test-Path $pathFile) {
        try {
            $published = (Get-Content $pathFile -Raw -ErrorAction Stop).Trim()
            if ($published) { return $published }
        }
        catch { }
    }

    # 2. Fall back to the app's own derivation.
    $flaskEnv = 'production'
    $envFile = Join-Path $RepoRoot '.env'
    if (Test-Path $envFile) {
        $match = Select-String -Path $envFile -Pattern '^\s*FLASK_ENV\s*=\s*(.+?)\s*$' -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($match) {
            $flaskEnv = $match.Matches[0].Groups[1].Value.Trim().Trim('"').Trim("'").ToLower()
        }
    }

    if ($flaskEnv -eq 'production' -and $env:LOCALAPPDATA) {
        return (Join-Path (Join-Path $env:LOCALAPPDATA 'SalesBuddy-data') 'salesbuddy.db')
    }
    return (Join-Path (Join-Path $RepoRoot 'data') 'salesbuddy.db')
}

function Backup-SalesBuddyDb {
    <#
    .SYNOPSIS
    Create a WAL-safe backup of the database by delegating to the shared Python
    helper (app/db_paths.py::backup_database), which uses the SQLite online-backup
    API so the snapshot folds in the WAL and is always consistent - unlike a plain
    Copy-Item, which can capture a torn or stale snapshot in WAL mode.

    Loads db_paths.py standalone (it imports only the stdlib) instead of the Flask
    app package, so it stays safe to call from a scheduled task / SYSTEM context.
    Returns $true on success, $false on failure.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$RepoRoot,
        [Parameter(Mandatory = $true)][string]$SourceDb,
        [Parameter(Mandatory = $true)][string]$DestFile
    )

    $pythonExe = Join-Path $RepoRoot 'venv\Scripts\python.exe'
    if (-not (Test-Path $pythonExe)) { $pythonExe = 'python' }
    $dbPathsFile = Join-Path $RepoRoot 'app\db_paths.py'
    if (-not (Test-Path $dbPathsFile)) { return $false }

    $py = @'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("db_paths", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
ok = mod.backup_database(sys.argv[3], src=sys.argv[2])
sys.exit(0 if ok else 1)
'@

    & $pythonExe -c $py $dbPathsFile $SourceDb $DestFile
    return ($LASTEXITCODE -eq 0)
}

