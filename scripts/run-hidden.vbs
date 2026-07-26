' Silent launcher for scheduled tasks.
' Runs a PowerShell script with no visible window and no console flash.
'
' Why this is more than "-WindowStyle Hidden":
'   On Windows 11 the default terminal ("Let Windows decide") resolves to
'   Windows Terminal. When a console app (powershell.exe) starts in an
'   INTERACTIVE session - which is how a per-user scheduled task runs when it
'   couldn't register as SYSTEM - Windows hands the console off to Windows
'   Terminal, which draws its OWN window and briefly flashes it. SW_HIDE does
'   not suppress that, because WT is a separate process. conhost.exe --headless
'   (Windows 11+) hosts the console with no window at all and bypasses the WT
'   handoff, so there is no flash. On Windows 10 (no --headless) we fall back to
'   a plain hidden PowerShell, which is already flash-free there.
'
' Exit code: conhost --headless does NOT propagate the child's exit code (it
' always returns 0). To keep a real success/failure signal for Task Scheduler,
' we wrap the child in cmd and record PowerShell's %errorlevel% to a sentinel
' file, then return that as this script's exit code. On Windows 10 we run
' PowerShell directly, which already propagates its exit code.
'
' Usage (from Task Scheduler):
'   wscript.exe "C:\path\to\run-hidden.vbs" "C:\path\to\script.ps1" [extra args...]

If WScript.Arguments.Count = 0 Then
    WScript.Quit 1
End If

Dim shell, fso, Q, scriptPath, extraArgs, psCommand, command, exitCode, i, sentinel, innerCmd
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
Q = Chr(34)
scriptPath = WScript.Arguments(0)

extraArgs = ""
For i = 1 To WScript.Arguments.Count - 1
    extraArgs = extraArgs & " " & Q & WScript.Arguments(i) & Q
Next

psCommand = "powershell.exe -ExecutionPolicy Bypass -NonInteractive -WindowStyle Hidden -File " & Q & scriptPath & Q & extraArgs

If SupportsHeadlessConhost() Then
    ' conhost --headless gives no window and no Windows Terminal handoff, but it
    ' swallows the child exit code. Wrap in cmd so PowerShell's real %errorlevel%
    ' is written to a sentinel we read back below. The (echo ...) form avoids
    ' cmd's "digit>" handle-redirect gotcha and leaves no trailing space.
    Randomize
    sentinel = shell.ExpandEnvironmentStrings("%TEMP%") & "\run-hidden-" & CLng(Timer * 1000) & "-" & Int(Rnd * 100000) & ".exit"
    If fso.FileExists(sentinel) Then fso.DeleteFile sentinel, True
    innerCmd = psCommand & " & (echo !errorlevel!)>" & Q & sentinel & Q
    command = "conhost.exe --headless cmd.exe /v:on /s /c " & Q & innerCmd & Q
    shell.Run command, 0, True
    exitCode = ReadExitSentinel(sentinel)
Else
    ' Windows 10: no --headless. Hidden PowerShell is already flash-free here and
    ' its process exit code propagates straight through.
    exitCode = shell.Run(psCommand, 0, True)
End If

WScript.Quit exitCode

' Read the exit code the cmd wrapper recorded. A missing/garbage sentinel means
' PowerShell never ran, so report failure (1).
Function ReadExitSentinel(path)
    Dim ts, raw
    ReadExitSentinel = 1
    On Error Resume Next
    If fso.FileExists(path) Then
        Set ts = fso.OpenTextFile(path, 1)
        raw = Trim(ts.ReadAll)
        ts.Close
        fso.DeleteFile path, True
        If IsNumeric(raw) Then ReadExitSentinel = CLng(raw)
    End If
    On Error GoTo 0
End Function

' Windows 11 is build 22000+. conhost --headless exists there; on older builds
' it is unrecognized and would fail to launch the child, so only use it on 11+.
Function SupportsHeadlessConhost()
    Dim items, item, build
    SupportsHeadlessConhost = False
    On Error Resume Next
    Set items = GetObject("winmgmts:\\.\root\cimv2").ExecQuery( _
        "SELECT BuildNumber FROM Win32_OperatingSystem")
    For Each item In items
        build = CLng(item.BuildNumber)
        If build >= 22000 Then SupportsHeadlessConhost = True
    Next
    On Error GoTo 0
End Function
