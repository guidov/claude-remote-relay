<#
.SYNOPSIS
    Register (or re-register) the claude-relay-bridge scheduled task.

.DESCRIPTION
    Creates a task with two triggers:

      * at logon      -- brings the bridge up after a reboot
      * every N mins  -- the heal trigger

    The heal trigger is what actually keeps the bridge alive, because Task
    Scheduler's RestartCount does NOT restart a task whose action exits. Paired
    with MultipleInstances=IgnoreNew, a repeating trigger is a restart policy:
    if the bridge is running the start is refused, and if it is not, the next
    firing takes over within N minutes.

    That refusal is recorded as last result 0x800710E0, once per interval, on a
    perfectly healthy machine. Do not go hunting for it.

    THIS SCRIPT REWRITES THE TASK. It is separate from check-bridge.ps1 on
    purpose: the two used to be one script, and running a "check" quietly
    re-registered the task and dropped the parts of the action it did not know
    about. Check freely; install deliberately.

.PARAMETER StderrLog
    Wrap the action in cmd.exe to capture the child's stderr to this file.

    Prefer leaving this unset. The bridge logs its own output, including a
    traceback for any unhandled exception, so the wrapper is now redundant --
    and it breaks Stop-ScheduledTask, which then stops cmd.exe while leaving
    python running. It is kept only for reproducing a machine already set up
    this way.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\windows\install-task.ps1

.EXAMPLE
    # Bind to the tailnet address rather than loopback:
    .\install-task.ps1 -BridgeHost 100.73.225.65
#>
[CmdletBinding(SupportsShouldProcess, ConfirmImpact = "High")]
param(
    [string] $TaskName    = "claude-relay-bridge",
    [string] $PythonExe,
    [int]    $HealMinutes = 1,

    # Written into the task as environment for the bridge to read. Left empty,
    # the bridge falls back to its own defaults (127.0.0.1:8787).
    [string] $BridgeHost,
    [int]    $Port,

    # See the note in .PARAMETER StderrLog before setting this.
    [string] $StderrLog,

    # Skip the confirmation prompt.
    [switch] $Force
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$bridge   = Join-Path $repoRoot "bridge\claude_bridge.py"
if (-not (Test-Path $bridge)) {
    throw "cannot find the bridge at $bridge -- run this from inside the checkout"
}

# --- python ----------------------------------------------------------------
if (-not $PythonExe) {
    $candidate = Get-Command python.exe -ErrorAction SilentlyContinue |
                 Where-Object { $_.Source -notlike "*WindowsApps*" } |
                 Select-Object -First 1
    if (-not $candidate) {
        throw "no python.exe on PATH (the WindowsApps stub does not count) -- pass -PythonExe"
    }
    $PythonExe = $candidate.Source
}
if (-not (Test-Path $PythonExe)) { throw "no python at $PythonExe" }

# A task runs with the service PATH, not yours. `claude` frequently is not on
# it, and the failure surfaces as a child that dies instantly with no message.
$claude = Get-Command claude.exe, claude.cmd -ErrorAction SilentlyContinue | Select-Object -First 1

# --- the action ------------------------------------------------------------
if ($StderrLog) {
    $execute  = "$env:SystemRoot\system32\cmd.exe"
    $argument = "/c `"$PythonExe`" `"$bridge`" 2>> `"$StderrLog`""
} else {
    $execute  = $PythonExe
    $argument = "`"$bridge`""
}

$action = New-ScheduledTaskAction -Execute $execute -Argument $argument -WorkingDirectory $repoRoot

$logon = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$heal  = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes($HealMinutes) `
            -RepetitionInterval (New-TimeSpan -Minutes $HealMinutes)

$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable

$description = "claude-remote-relay bridge. Self-heals via a ${HealMinutes}-min " +
               "repeating trigger + MultipleInstances=IgnoreNew; session_id persists."

# --- say what will happen, then do it --------------------------------------
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "about to register '$TaskName':" -ForegroundColor Cyan
Write-Host "  execute   : $execute"
Write-Host "  arguments : $argument"
Write-Host "  workdir   : $repoRoot"
Write-Host "  triggers  : at logon, and every $HealMinutes min"
if ($BridgeHost -or $Port) {
    Write-Host "  NOTE: -BridgeHost/-Port are recorded here for reference only." -ForegroundColor Yellow
    Write-Host "        Task Scheduler has no environment block; set" -ForegroundColor Yellow
    Write-Host "        CLAUDE_BRIDGE_HOST/PORT as user environment variables" -ForegroundColor Yellow
    Write-Host "        (setx) or the bridge will not see them." -ForegroundColor Yellow
}
if (-not $claude) {
    Write-Host "  WARNING: no claude.exe on PATH. If the task's PATH also lacks it," -ForegroundColor Yellow
    Write-Host "           set CLAUDE_BRIDGE_CLAUDE_BIN to its full path." -ForegroundColor Yellow
}
if ($existing) {
    Write-Host ""
    Write-Host "  this REPLACES the existing task, whose action is currently:" -ForegroundColor Yellow
    foreach ($a in $existing.Actions) {
        Write-Host ("    {0} {1}" -f $a.Execute, $a.Arguments) -ForegroundColor Yellow
    }
    Write-Host "  anything in that action not reproduced above will be lost." -ForegroundColor Yellow
}
Write-Host ""

if (-not ($Force -or $PSCmdlet.ShouldProcess($TaskName, "register scheduled task"))) {
    Write-Host "aborted; nothing was changed." -ForegroundColor Yellow
    return
}

Register-ScheduledTask -TaskName $TaskName -Description $description `
    -Action $action -Trigger @($logon, $heal) -Settings $settings `
    -RunLevel Limited -Force | Out-Null

$now = Get-ScheduledTask -TaskName $TaskName
Write-Host "registered. triggers now:" -ForegroundColor Green
$now.Triggers | ForEach-Object {
    Write-Host ("  {0,-28} repeat={1}" -f $_.CimClass.CimClassName, $_.Repetition.Interval)
}
Write-Host ""
Write-Host "The heal trigger starts it within $HealMinutes min. To bring it up now:" -ForegroundColor Cyan
Write-Host "  Start-ScheduledTask -TaskName $TaskName"
Write-Host "Then verify with:" -ForegroundColor Cyan
Write-Host "  .\check-bridge.ps1"
