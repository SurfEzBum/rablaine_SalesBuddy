<#
.SYNOPSIS
    Migrates an existing Flask-mode Sales Buddy install to the Electron desktop shell.
.DESCRIPTION
    "Run it and walk away" migration. In order it:
      1. Locates this install (the repo this script lives in) and the packaged,
         signed Electron shell.
      2. (Optional) git pull --ff-only to bring the backend current.
      3. Stops the running Flask stack (supervisor + web + worker).
      4. Stages the Electron shell into <install>\electron-dist\.
      5. Repoints the ON LOGON scheduled task to launch Electron (delete-before-create,
         so there is never a second login launcher fighting over the port).
      6. Replaces the desktop + Start Menu shortcuts to point at Electron.
      7. Launches Electron now - the window and tray appear immediately.

    Idempotent: safe to re-run. Leaves the SalesBuddy-DailyBackup task untouched.
.PARAMETER ElectronSource
    Path to the built, signed Electron app - the electron-builder "win-unpacked"
    folder containing "Sales Buddy.exe". Defaults to <repo>\electron\dist\win-unpacked
    (local build). For rollout this is where a downloaded release would be pointed.
.PARAMETER SkipPull
    Skip the git pull step.
.EXAMPLE
    .\migrate-to-electron.ps1
    .\migrate-to-electron.ps1 -ElectronSource D:\downloads\SalesBuddy-shell\win-unpacked
#>
param(
    [string]$ElectronSource,
    [switch]$SkipPull
)

$ErrorActionPreference = 'Stop'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $scriptDir
$appName = 'Sales Buddy'
$exeName = 'Sales Buddy.exe'
$taskName = 'SalesBuddy-AutoStart'
$destDir = Join-Path $repoRoot 'electron-dist'

Write-Host "=== Migrate Sales Buddy to the Electron desktop app ===" -ForegroundColor Cyan
Write-Host "Install: $repoRoot"

# --- 1. Locate the packaged Electron shell ---
if (-not $ElectronSource) {
    $ElectronSource = Join-Path $repoRoot 'electron\dist\win-unpacked'
}
$srcExe = Join-Path $ElectronSource $exeName
if (-not (Test-Path $srcExe)) {
    Write-Host "`nERROR: packaged Electron shell not found at:" -ForegroundColor Red
    Write-Host "  $srcExe" -ForegroundColor Red
    Write-Host "Build it first (electron\build.ps1) or pass -ElectronSource <win-unpacked path>." -ForegroundColor Yellow
    exit 1
}

# --- 2. Bring the backend current (best effort) ---
if (-not $SkipPull) {
    Write-Host "`nUpdating backend (git pull)..." -ForegroundColor Yellow
    Push-Location $repoRoot
    try {
        & git pull --ff-only 2>&1 | Write-Host
        if ($LASTEXITCODE -ne 0) {
            Write-Host "  git pull skipped/failed (continuing with current code)." -ForegroundColor DarkYellow
        }
    } catch {
        Write-Host "  git not available (continuing)." -ForegroundColor DarkYellow
    } finally {
        Pop-Location
    }
}

# --- 3. Stop the running Flask stack ---
Write-Host "`nStopping the running Sales Buddy stack..." -ForegroundColor Yellow
$serverScript = Join-Path $repoRoot 'scripts\server.ps1'
if (Test-Path $serverScript) {
    try { & $serverScript -StopOnly 2>&1 | Out-Null } catch { }
}
# Belt and suspenders: tree-kill anything from THIS install still running
# (scoped to the install path so we never touch another install or unrelated python).
Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe' OR Name='waitress-serve.exe' OR Name='Sales Buddy.exe'" |
    Where-Object { $_.CommandLine -and $_.CommandLine -match [regex]::Escape($repoRoot) } |
    ForEach-Object { taskkill /PID $_.ProcessId /T /F 2>$null | Out-Null }

# --- 4. Stage the signed shell into <install>\electron-dist\ ---
Write-Host "Staging Electron shell -> $destDir" -ForegroundColor Yellow
if (Test-Path $destDir) { Remove-Item $destDir -Recurse -Force }
New-Item -ItemType Directory -Path $destDir | Out-Null
Copy-Item -Path (Join-Path $ElectronSource '*') -Destination $destDir -Recurse -Force
$destExe = Join-Path $destDir $exeName
if (-not (Test-Path $destExe)) {
    Write-Host "ERROR: staging failed, $destExe missing." -ForegroundColor Red
    exit 1
}

# --- 5. Repoint the ON LOGON scheduled task to Electron (delete-before-create) ---
Write-Host "Repointing login task '$taskName' -> Electron..." -ForegroundColor Yellow
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
$action = New-ScheduledTaskAction -Execute $destExe
$trigger = New-ScheduledTaskTrigger -AtLogOn
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force | Out-Null

# --- 6. Replace shortcuts (remove old browser links, add Electron ones) ---
Write-Host "Updating desktop + Start Menu shortcuts..." -ForegroundColor Yellow
$icon = Join-Path $repoRoot 'static\icon.ico'
$desktop = [Environment]::GetFolderPath('Desktop')
$startMenu = Join-Path ([Environment]::GetFolderPath('ApplicationData')) `
    'Microsoft\Windows\Start Menu\Programs\Sales Buddy'
$shell = New-Object -ComObject WScript.Shell

function New-AppShortcut {
    param($LnkPath, $Target, $IconPath, $Description)
    $sc = $shell.CreateShortcut($LnkPath)
    $sc.TargetPath = $Target
    $sc.WorkingDirectory = Split-Path -Parent $Target
    if (Test-Path $IconPath) { $sc.IconLocation = "$IconPath,0" }
    $sc.Description = $Description
    $sc.Save()
}

# Remove the old "web" shortcuts (explorer.exe -> http://localhost).
Remove-Item (Join-Path $desktop "$appName (web).lnk") -Force -ErrorAction SilentlyContinue
Remove-Item (Join-Path $startMenu "$appName (web).lnk") -Force -ErrorAction SilentlyContinue

# Create the new Electron shortcuts.
if (-not (Test-Path $startMenu)) { New-Item -ItemType Directory -Path $startMenu | Out-Null }
New-AppShortcut (Join-Path $desktop "$appName.lnk") $destExe $icon 'Open Sales Buddy'
New-AppShortcut (Join-Path $startMenu "$appName.lnk") $destExe $icon 'Open Sales Buddy'

# --- 7. Launch Electron now ---
Write-Host "Launching Sales Buddy..." -ForegroundColor Yellow
Start-Process -FilePath $destExe

Write-Host "`n=== Migration complete ===" -ForegroundColor Green
Write-Host "Sales Buddy now runs as a desktop app (system tray + window)." -ForegroundColor Green
Write-Host "It will start automatically the next time you log in." -ForegroundColor Green
