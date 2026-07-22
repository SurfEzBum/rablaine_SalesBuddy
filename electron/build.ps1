<#
.SYNOPSIS
    Builds and signs the Sales Buddy Electron desktop shell.
.DESCRIPTION
    Runs electron-builder to produce an unpacked app directory, then code-signs
    the binaries with Azure Trusted Signing - the same cert and toolchain the
    MSI uses (installer/build.ps1 + installer/signing-metadata.json).

    Output: electron/dist/win-unpacked/  (contains "Sales Buddy.exe"). This
    folder is what the MSI harvests and what scripts/migrate-to-electron.ps1
    drops into <install>\electron-dist\.
.PARAMETER SkipInstall
    Skip "npm install" (use when node_modules is already present).
.PARAMETER SkipSign
    Build only; do not code-sign (useful for local dev builds).
.EXAMPLE
    .\build.ps1
    .\build.ps1 -SkipSign
#>
param(
    [switch]$SkipInstall,
    [switch]$SkipSign
)

$ErrorActionPreference = 'Stop'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path   # electron/
$repoRoot = Split-Path -Parent $scriptDir

Write-Host "=== Sales Buddy Electron Build ===" -ForegroundColor Cyan

# --- Node / npm ---
if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
    Write-Host "ERROR: npm not found. Install Node.js (https://nodejs.org)." -ForegroundColor Red
    exit 1
}

Push-Location $scriptDir
try {
    if (-not $SkipInstall) {
        Write-Host "`nInstalling npm dependencies..." -ForegroundColor Yellow
        & npm install
        if ($LASTEXITCODE -ne 0) { throw "npm install failed" }
    }

    Write-Host "`nBuilding unpacked app (electron-builder --dir)..." -ForegroundColor Yellow
    & npx electron-builder --dir
    if ($LASTEXITCODE -ne 0) { throw "electron-builder failed" }
} finally {
    Pop-Location
}

$unpacked = Join-Path $scriptDir 'dist\win-unpacked'
$exe = Join-Path $unpacked 'Sales Buddy.exe'
if (-not (Test-Path $exe)) {
    Write-Host "ERROR: expected launcher not found at $exe" -ForegroundColor Red
    exit 1
}
Write-Host "`nBuilt: $exe" -ForegroundColor Green

# --- Stamp exe resources (icon + product name/version) ---
# electron-builder runs with signAndEditExecutable=false so it never downloads
# winCodeSign (its archive contains macOS symlinks that fail to extract on a
# stock Windows box without Developer Mode - the same failure this MSI build
# would hit on every user machine). The cost of skipping that step is an
# unbranded exe: Sales Buddy.exe keeps Electron's default icon and "Electron"
# product name, so Windows shows "Electron" + a generic icon in the taskbar
# jump list and drops our icon when the app is pinned. We stamp the resources
# ourselves with the standalone rcedit tool (bundles its own exe, no winCodeSign)
# BEFORE signing, so the signature covers the final, branded binary.
$rcedit = Join-Path $scriptDir 'node_modules\rcedit\bin\rcedit-x64.exe'
$icon = Join-Path $repoRoot 'static\icon.ico'
if ((Test-Path $rcedit) -and (Test-Path $icon)) {
    $ver = (Get-Content (Join-Path $scriptDir 'package.json') -Raw | ConvertFrom-Json).version
    $fileVer = if ($ver -match '^\d+\.\d+\.\d+$') { "$ver.0" } else { $ver }
    Write-Host "`nStamping exe resources (icon + product info)..." -ForegroundColor Yellow
    & $rcedit "$exe" `
        --set-icon "$icon" `
        --set-version-string "ProductName" "Sales Buddy" `
        --set-version-string "FileDescription" "Sales Buddy" `
        --set-version-string "CompanyName" "Sales Buddy" `
        --set-version-string "InternalName" "Sales Buddy" `
        --set-version-string "OriginalFilename" "Sales Buddy.exe" `
        --set-version-string "LegalCopyright" "Sales Buddy" `
        --set-file-version "$fileVer" `
        --set-product-version "$fileVer"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "WARNING: rcedit failed - exe keeps Electron's default icon/name." -ForegroundColor Red
    } else {
        Write-Host "  Stamped icon + product info (Sales Buddy $fileVer)." -ForegroundColor Green
    }
} else {
    Write-Host "`nWARNING: skipping resource stamp - exe keeps Electron branding." -ForegroundColor Red
    if (-not (Test-Path $rcedit)) { Write-Host "  Missing: rcedit (run npm install)." -ForegroundColor DarkYellow }
    if (-not (Test-Path $icon)) { Write-Host "  Missing: $icon" -ForegroundColor DarkYellow }
}

# --- Sign (Azure Trusted Signing, mirrors installer/build.ps1) ---
if ($SkipSign) {
    Write-Host "`nSkipping signing (-SkipSign). Output is unsigned." -ForegroundColor Yellow
    Write-Host "`n=== Electron build complete ===" -ForegroundColor Cyan
    Write-Host "Output: $unpacked" -ForegroundColor Green
    exit 0
}

$signtool = "C:\Program Files (x86)\Windows Kits\10\bin\10.0.26100.0\x64\signtool.exe"
$dlib = "$env:LOCALAPPDATA\Microsoft\MicrosoftArtifactSigningClientTools\Azure.CodeSigning.Dlib.dll"
$metadata = Join-Path $repoRoot 'installer\signing-metadata.json'

if ((Test-Path $signtool) -and (Test-Path $dlib) -and (Test-Path $metadata)) {
    Write-Host "`nSigning binaries with Azure Trusted Signing..." -ForegroundColor Yellow
    # Sign the launcher exe plus the bundled native binaries so enterprise AV /
    # SmartScreen see a fully-signed app folder.
    $targets = Get-ChildItem -Path $unpacked -Recurse -Include *.exe, *.dll, *.node -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty FullName
    if ($targets) {
        & $signtool sign /v /fd SHA256 /tr "http://timestamp.acs.microsoft.com" /td SHA256 /dlib $dlib /dmdf $metadata @targets
        if ($LASTEXITCODE -ne 0) {
            Write-Host "WARNING: signing failed. Output is unsigned." -ForegroundColor Red
        } else {
            Write-Host "  Signed $($targets.Count) file(s)." -ForegroundColor Green
        }
    }
} else {
    Write-Host "`nSkipping code signing (tools not installed)." -ForegroundColor Yellow
    if (-not (Test-Path $signtool)) { Write-Host "  Missing: signtool.exe" -ForegroundColor DarkYellow }
    if (-not (Test-Path $dlib)) { Write-Host "  Missing: Azure.CodeSigning.Dlib.dll" -ForegroundColor DarkYellow }
    if (-not (Test-Path $metadata)) { Write-Host "  Missing: signing-metadata.json" -ForegroundColor DarkYellow }
}

Write-Host "`n=== Electron build complete ===" -ForegroundColor Cyan
Write-Host "Output: $unpacked" -ForegroundColor Green
