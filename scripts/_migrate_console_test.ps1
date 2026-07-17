<#
.SYNOPSIS
    Dev-only wiring test for the "Move to desktop app" button.
.DESCRIPTION
    Does NOT migrate anything. It just prints to a visible console (with a bit
    of live/streaming output) so you can confirm that clicking the button in the
    admin Updates card successfully launches a window whose output you can watch.

    In development mode, POST /api/admin/migrate-to-electron launches THIS script
    instead of the real migrate-to-electron.ps1. The endpoint wraps it with a
    "press Enter to close" pause, so this script does not pause on its own.
#>

Write-Host ''
Write-Host '  ============================================================' -ForegroundColor Cyan
Write-Host '   Sales Buddy - "Move to desktop app" wiring test' -ForegroundColor Cyan
Write-Host '  ============================================================' -ForegroundColor Cyan
Write-Host ''
Write-Host '  If you can read this, the wiring works:' -ForegroundColor Green
Write-Host '    button click -> /api/admin/migrate-to-electron ->' -ForegroundColor Green
Write-Host '    a visible console window you can watch.' -ForegroundColor Green
Write-Host ''
Write-Host '  In production this same path launches the real migration,' -ForegroundColor DarkGray
Write-Host '  which builds the desktop app and switches you over.' -ForegroundColor DarkGray
Write-Host ''
Write-Host "  Working dir : $PWD"
Write-Host "  Timestamp   : $(Get-Date -Format o)"
Write-Host ''

# Simulate a little streaming work so you can see live output, like the real
# build would produce.
1..3 | ForEach-Object {
    Write-Host "  ...working ($_/3)" -ForegroundColor Yellow
    Start-Sleep -Seconds 1
}

Write-Host ''
Write-Host '  Test complete - the wiring is good.' -ForegroundColor Green
