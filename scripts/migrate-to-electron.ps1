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
    [switch]$SkipPull,
    [switch]$Rebuild,
    [switch]$NoDesktop,
    [switch]$NoStartMenu,
    [switch]$NoAutoStart,
    [switch]$NoLaunch
)

$ErrorActionPreference = 'Stop'
# Native commands (schtasks, git, build.ps1) may return non-zero or write to
# stderr for benign reasons (e.g. deleting a task that doesn't exist). Do NOT let
# that abort the whole migration - we check exit codes explicitly where it matters.
$PSNativeCommandUseErrorActionPreference = $false
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $scriptDir
$appName = 'Sales Buddy'
$exeName = 'Sales Buddy.exe'
$taskName = 'SalesBuddy-AutoStart'
$destDir = Join-Path $repoRoot 'electron-dist'

Write-Host "=== Migrate Sales Buddy to the Electron desktop app ===" -ForegroundColor Cyan
Write-Host "Install: $repoRoot"

# --- 1. Locate (or build) the packaged Electron shell ---
# Option 1 distribution: the shell is built locally. Node is present on every
# install (the installer ships it as a prereq), so we can build on demand.
if (-not $ElectronSource) {
    $ElectronSource = Join-Path $repoRoot 'electron\dist\win-unpacked'
}
# -Rebuild forces a fresh build from the CURRENT source. The MSI passes this on
# every install/upgrade: the repo was just reset to origin/main, but the old
# win-unpacked is gitignored (survives `git clean -fd`), so without this we'd
# re-stage the stale shell and never pick up new main.js. Only ever removes the
# local build output, never an explicitly supplied -ElectronSource.
if ($Rebuild -and -not $PSBoundParameters.ContainsKey('ElectronSource')) {
    if (Test-Path $ElectronSource) {
        Write-Host "Rebuild requested - removing stale shell build..." -ForegroundColor Yellow
        Remove-Item $ElectronSource -Recurse -Force -ErrorAction SilentlyContinue
    }
}
$srcExe = Join-Path $ElectronSource $exeName
if (-not (Test-Path $srcExe)) {
    $buildScript = Join-Path $repoRoot 'electron\build.ps1'
    if (Test-Path $buildScript) {
        Write-Host "`nElectron shell not built yet - building it (this may take a minute)..." -ForegroundColor Yellow
        & $buildScript -SkipSign
        if ($LASTEXITCODE -ne 0) {
            Write-Host "ERROR: Electron build failed." -ForegroundColor Red
            exit 1
        }
    }
}
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
# Killing the shell above does NOT release its file handles instantly. Staging
# before the lock clears is what corrupts electron-dist (a partial delete/copy
# drops files like icudtl.dat -> "Invalid ICU data" crash on launch). So: wait
# for the old exe to become deletable, then stage with retry + a completeness
# check (file count + the two files whose absence we've actually been burned by).
Write-Host "Staging Electron shell -> $destDir" -ForegroundColor Yellow

# First make sure the SOURCE build is COMPLETE and STABLE. electron-builder
# finalizes win-unpacked asynchronously (asar integrity, native dlls), and it
# can return before the last files are flushed. Staging mid-finalization is what
# produced an empty/partial electron-dist -> web fallback. Wait for exe +
# icudtl.dat AND a file count that has stopped changing (>= 50 files).
$prevCount = -1
for ($w = 0; $w -lt 120; $w++) {
    $c = (Get-ChildItem $ElectronSource -Recurse -File -ErrorAction SilentlyContinue).Count
    if ((Test-Path (Join-Path $ElectronSource $exeName)) -and
        (Test-Path (Join-Path $ElectronSource 'icudtl.dat')) -and
        ($c -ge 50) -and ($c -eq $prevCount)) { break }
    $prevCount = $c
    Start-Sleep -Milliseconds 500
}
$srcFileCount = (Get-ChildItem $ElectronSource -Recurse -File -ErrorAction SilentlyContinue).Count
if (-not ((Test-Path (Join-Path $ElectronSource $exeName)) -and
          (Test-Path (Join-Path $ElectronSource 'icudtl.dat')) -and ($srcFileCount -ge 50))) {
    Write-Host "ERROR: shell build source is incomplete ($srcFileCount files)." -ForegroundColor Red
    exit 1
}

$destExe = Join-Path $destDir $exeName
if (Test-Path $destExe) {
    for ($w = 0; $w -lt 20; $w++) {
        try {
            $probe = [System.IO.File]::Open($destExe, 'Open', 'ReadWrite', 'None')
            $probe.Close(); $probe.Dispose()
            break  # got an exclusive handle -> the shell has fully released it
        } catch { Start-Sleep -Milliseconds 500 }
    }
}
$staged = $false
for ($attempt = 1; $attempt -le 3; $attempt++) {
    if (Test-Path $destDir) { Remove-Item $destDir -Recurse -Force -ErrorAction SilentlyContinue }
    if (-not (Test-Path $destDir)) { New-Item -ItemType Directory -Path $destDir | Out-Null }
    # robocopy, not Copy-Item: it natively retries files that are briefly locked
    # (antivirus scanning the freshly-built exe/dlls under load), which is what
    # made Copy-Item silently skip files and leave an incomplete/empty stage.
    # /R:8 /W:2 = up to 8 retries, 2s apart, per locked file. Exit codes 0-7 are
    # success (files copied / nothing to do); >=8 is a real failure.
    & robocopy $ElectronSource $destDir /E /R:8 /W:2 /NFL /NDL /NJH /NJS /NP *> $null
    $roboExit = $LASTEXITCODE
    $dstFileCount = (Get-ChildItem $destDir -Recurse -File -ErrorAction SilentlyContinue).Count
    if (($roboExit -lt 8) -and
        (Test-Path (Join-Path $destDir $exeName)) -and
        (Test-Path (Join-Path $destDir 'icudtl.dat')) -and
        ($dstFileCount -ge $srcFileCount)) {
        $staged = $true
        break
    }
    Write-Host "  Stage incomplete (robocopy=$roboExit, $dstFileCount/$srcFileCount files) - retrying..." -ForegroundColor DarkYellow
    Start-Sleep -Seconds 2
}
if (-not $staged) {
    Write-Host "ERROR: could not fully stage the desktop shell after 3 attempts." -ForegroundColor Red
    exit 1
}

# --- 5. Set login autostart -> Electron (per-user, no elevation) ---
# Use an HKCU\...\Run entry instead of a scheduled task. The old Flask autostart
# task was created by the MSI running ELEVATED, so this migration - which runs
# non-elevated when triggered from the web "Move to desktop app" button - can't
# create or overwrite a root scheduled task (Access denied 0x80070005). HKCU\Run
# is the user's own hive (no elevation) and launches the GUI exe with no console
# flash. We still remove the legacy scheduled task so there's exactly one launcher.
if ($NoAutoStart) {
    Write-Host "Skipping auto-start (-NoAutoStart)." -ForegroundColor DarkYellow
} else {
    Write-Host "Setting login autostart -> Electron..." -ForegroundColor Yellow
    # Remove the legacy Flask scheduled task if present. Route through cmd with
    # output fully suppressed so a "task not found" (stderr + exit 1) can never
    # abort the migration before we set autostart / shortcuts.
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    try { & cmd.exe /c "schtasks /delete /tn ""$taskName"" /f >nul 2>&1" } catch {}
    # Per-user Run entry -> launch the Electron exe at login.
    $runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
    New-ItemProperty -Path $runKey -Name 'SalesBuddy' -Value ('"{0}"' -f $destExe) `
        -PropertyType String -Force | Out-Null
}

# --- 6. Replace shortcuts (remove old browser links, add Electron ones) ---
Write-Host "Updating desktop + Start Menu shortcuts..." -ForegroundColor Yellow
$icon = Join-Path $repoRoot 'static\icon.ico'
$desktop = [Environment]::GetFolderPath('Desktop')
$startMenu = Join-Path ([Environment]::GetFolderPath('ApplicationData')) `
    'Microsoft\Windows\Start Menu\Programs\Sales Buddy'
$shell = New-Object -ComObject WScript.Shell

# Windows only merges a running app's taskbar button with a shortcut when the
# shortcut's System.AppUserModel.ID matches the AUMID the app sets at runtime
# (electron/main.js: app.setAppUserModelId('com.salesbuddy.desktop')). Our
# shortcuts previously had only the implicit AUMID (their target exe path), which
# never matches, so pinning the running app spawned a stray "Electron" pin that
# kept Electron's name + icon. WScript.Shell can't set that property, so we set it
# through the shell IPropertyStore API (PKEY_AppUserModel_ID).
$appUserModelId = 'com.salesbuddy.desktop'
if (-not ('SalesBuddy.ShortcutProps' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
namespace SalesBuddy {
    public static class ShortcutProps {
        public static void SetAppUserModelId(string shortcutPath, string appId) {
            IShellLinkW link = (IShellLinkW)new CShellLink();
            IPersistFile file = (IPersistFile)link;
            file.Load(shortcutPath, 2); // STGM_READWRITE
            IPropertyStore store = (IPropertyStore)link;
            PROPERTYKEY key = new PROPERTYKEY();
            key.fmtid = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3");
            key.pid = 5;
            PROPVARIANT pv = new PROPVARIANT();
            pv.vt = 31; // VT_LPWSTR
            pv.data = Marshal.StringToCoTaskMemUni(appId);
            store.SetValue(ref key, ref pv);
            store.Commit();
            Marshal.FreeCoTaskMem(pv.data);
            file.Save(shortcutPath, true);
            Marshal.ReleaseComObject(store);
            Marshal.ReleaseComObject(file);
            Marshal.ReleaseComObject(link);
        }
    }
    [ComImport, Guid("00021401-0000-0000-C000-000000000046")]
    internal class CShellLink { }
    [ComImport, InterfaceType(ComInterfaceType.InterfaceIsIUnknown),
     Guid("000214F9-0000-0000-C000-000000000046")]
    internal interface IShellLinkW {
        void GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] System.Text.StringBuilder f, int cch, IntPtr pfd, uint fl);
        void GetIDList(out IntPtr ppidl);
        void SetIDList(IntPtr pidl);
        void GetDescription([Out, MarshalAs(UnmanagedType.LPWStr)] System.Text.StringBuilder n, int cch);
        void SetDescription([MarshalAs(UnmanagedType.LPWStr)] string n);
        void GetWorkingDirectory([Out, MarshalAs(UnmanagedType.LPWStr)] System.Text.StringBuilder d, int cch);
        void SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string d);
        void GetArguments([Out, MarshalAs(UnmanagedType.LPWStr)] System.Text.StringBuilder a, int cch);
        void SetArguments([MarshalAs(UnmanagedType.LPWStr)] string a);
        void GetHotkey(out short w);
        void SetHotkey(short w);
        void GetShowCmd(out int c);
        void SetShowCmd(int c);
        void GetIconLocation([Out, MarshalAs(UnmanagedType.LPWStr)] System.Text.StringBuilder p, int cch, out int i);
        void SetIconLocation([MarshalAs(UnmanagedType.LPWStr)] string p, int i);
        void SetRelativePath([MarshalAs(UnmanagedType.LPWStr)] string r, uint dw);
        void Resolve(IntPtr hwnd, uint fl);
        void SetPath([MarshalAs(UnmanagedType.LPWStr)] string f);
    }
    [ComImport, InterfaceType(ComInterfaceType.InterfaceIsIUnknown),
     Guid("0000010b-0000-0000-C000-000000000046")]
    internal interface IPersistFile {
        void GetClassID(out Guid c);
        [PreserveSig] int IsDirty();
        void Load([MarshalAs(UnmanagedType.LPWStr)] string f, int m);
        void Save([MarshalAs(UnmanagedType.LPWStr)] string f, [MarshalAs(UnmanagedType.Bool)] bool r);
        void SaveCompleted([MarshalAs(UnmanagedType.LPWStr)] string f);
        void GetCurFile([MarshalAs(UnmanagedType.LPWStr)] out string f);
    }
    [ComImport, InterfaceType(ComInterfaceType.InterfaceIsIUnknown),
     Guid("886d8eeb-8cf2-4446-8d02-cdba1dbdcf99")]
    internal interface IPropertyStore {
        void GetCount(out uint c);
        void GetAt(uint i, out PROPERTYKEY k);
        void GetValue(ref PROPERTYKEY k, out PROPVARIANT pv);
        void SetValue(ref PROPERTYKEY k, ref PROPVARIANT pv);
        void Commit();
    }
    [StructLayout(LayoutKind.Sequential)]
    internal struct PROPERTYKEY { public Guid fmtid; public uint pid; }
    [StructLayout(LayoutKind.Explicit)]
    internal struct PROPVARIANT {
        [FieldOffset(0)] public ushort vt;
        [FieldOffset(8)] public IntPtr data;
    }
}
'@
}

function New-AppShortcut {
    param($LnkPath, $Target, $IconPath, $Description)
    $sc = $shell.CreateShortcut($LnkPath)
    $sc.TargetPath = $Target
    $sc.WorkingDirectory = Split-Path -Parent $Target
    if (Test-Path $IconPath) { $sc.IconLocation = "$IconPath,0" }
    $sc.Description = $Description
    $sc.Save()
    # Stamp the AUMID so Windows ties the pinned/taskbar button to this shortcut.
    try { [SalesBuddy.ShortcutProps]::SetAppUserModelId($LnkPath, $appUserModelId) }
    catch { Write-Host "  (couldn't set AppUserModelID on $LnkPath): $($_.Exception.Message)" -ForegroundColor DarkYellow }
}

# Remove the old "web" shortcuts (explorer.exe -> http://localhost).
Remove-Item (Join-Path $desktop "$appName (web).lnk") -Force -ErrorAction SilentlyContinue
Remove-Item (Join-Path $startMenu "$appName (web).lnk") -Force -ErrorAction SilentlyContinue

# Create the new Electron shortcuts.
if (-not $NoDesktop) {
    New-AppShortcut (Join-Path $desktop "$appName.lnk") $destExe $icon 'Open Sales Buddy'
}
if (-not $NoStartMenu) {
    if (-not (Test-Path $startMenu)) { New-Item -ItemType Directory -Path $startMenu | Out-Null }
    New-AppShortcut (Join-Path $startMenu "$appName.lnk") $destExe $icon 'Open Sales Buddy'
}

# --- 6.5 Verify the backend venv is usable; self-heal if deps are missing ---
# A prior interrupted install can leave a fresh venv with no packages
# ("No module named flask"), which crash-loops the supervisor forever. Cheap
# import probe; only reinstalls when actually broken so healthy installs stay fast.
$venvPy = Join-Path $repoRoot 'venv\Scripts\python.exe'
$reqFile = Join-Path $repoRoot 'requirements.txt'
if ((Test-Path $venvPy) -and (Test-Path $reqFile)) {
    & $venvPy -c "import flask" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Backend dependencies missing - installing (one-time)..." -ForegroundColor Yellow
        & $venvPy -m pip install -q -r $reqFile
    }
}

# --- 7. Launch Electron now ---
if ($NoLaunch) {
    Write-Host "Skipping launch (-NoLaunch)." -ForegroundColor DarkYellow
} else {
    Write-Host "Launching Sales Buddy..." -ForegroundColor Yellow
    Start-Process -FilePath $destExe
}

Write-Host "`n=== Migration complete ===" -ForegroundColor Green
Write-Host "Sales Buddy now runs as a desktop app (system tray + window)." -ForegroundColor Green
Write-Host "It will start automatically the next time you log in." -ForegroundColor Green
