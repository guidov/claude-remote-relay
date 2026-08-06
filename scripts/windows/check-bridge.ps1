<#
.SYNOPSIS
    Read-only health check for the claude-relay-bridge scheduled task.

.DESCRIPTION
    Reports whether the bridge is serving, whether the saved session survived,
    what the log says since the last boot, and whether any `claude` child
    outlived its parent.

    This script only reads. It deliberately does NOT register or restart the
    scheduled task -- an earlier version of it re-registered the task at the end
    of every run, which silently rewrote the task action and cost the stderr
    redirect. Installing is install-task.ps1's job, and you have to ask for it.

    The expected version is read from the source tree rather than hardcoded. A
    hardcoded constant here goes stale the moment the bridge is bumped and then
    reports a correct deploy as a failed one.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\windows\check-bridge.ps1

.EXAMPLE
    # After a reboot, confirming a specific conversation came back:
    .\check-bridge.ps1 -SessionId 990b4808-6059-4df7-8b6f-321915938f31
#>
[CmdletBinding()]
param(
    # Where the bridge should be listening. Defaults to the same environment
    # variables the bridge itself reads, so a machine configured once agrees.
    [string] $BridgeHost = $(if ($env:CLAUDE_BRIDGE_HOST) { $env:CLAUDE_BRIDGE_HOST } else { "127.0.0.1" }),
    [int]    $Port       = $(if ($env:CLAUDE_BRIDGE_PORT) { [int]$env:CLAUDE_BRIDGE_PORT } else { 8787 }),

    [string] $TaskName   = "claude-relay-bridge",
    [string] $TokenFile  = "$HOME\.claude-relay-token",
    [string] $LogFile    = $(if ($env:CLAUDE_BRIDGE_LOG) { $env:CLAUDE_BRIDGE_LOG } else { "$HOME\claude-relay.log" }),
    [string] $StderrLog  = "$HOME\claude-relay-stderr.log",

    # If given, the check asserts this conversation came back after the reboot.
    [string] $SessionId,

    # The bind retries for up to CLAUDE_BRIDGE_BIND_RETRY_S (900s by default),
    # so a bridge that is not answering yet may still be waiting on the tailnet
    # address. Poll a little before calling it dead, then read the log.
    [int]    $PollSeconds = 60
)

$ErrorActionPreference = "Continue"

$repoRoot  = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$source    = Join-Path $repoRoot "bridge\claude_bridge.py"
$baseUrl   = "http://{0}:{1}" -f $BridgeHost, $Port

function Write-Section($text) {
    Write-Host ""
    Write-Host ("=== {0} {1}" -f $text, ("=" * [Math]::Max(0, 55 - $text.Length))) -ForegroundColor Cyan
}

# --- the version the source says we should be serving ----------------------
$want = $null
if (Test-Path $source) {
    $m = Select-String -Path $source -Pattern '^VERSION\s*=\s*"([^"]+)"' | Select-Object -First 1
    if ($m) { $want = $m.Matches[0].Groups[1].Value }
}

# --- boot ------------------------------------------------------------------
$boot = (Get-CimInstance Win32_OperatingSystem).LastBootUpTime
Write-Section "boot"
Write-Host "last boot : $boot"
Write-Host "now       : $(Get-Date)"

# --- health ----------------------------------------------------------------
Write-Section "health (polling up to ${PollSeconds}s)"
if (-not (Test-Path $TokenFile)) {
    Write-Host "no token file at $TokenFile -- cannot authenticate" -ForegroundColor Red
    return
}
$hdr = @{ Authorization = "Bearer $((Get-Content $TokenFile -Raw).Trim())" }

$up = $null
$deadline = (Get-Date).AddSeconds($PollSeconds)
do {
    try { $up = Invoke-RestMethod -Uri "$baseUrl/health" -Headers $hdr -TimeoutSec 5 } catch { }
    if ($up) { break }
    Start-Sleep -Seconds 5
} while ((Get-Date) -lt $deadline)

if ($up) {
    Write-Host "BRIDGE IS UP" -ForegroundColor Green
    Write-Host ("  version       : {0}"  -f $up.version)
    Write-Host ("  name          : {0}"  -f $up.name)
    Write-Host ("  session_state : {0}"  -f $up.session_state)
    Write-Host ("  resuming      : {0}"  -f $up.resuming)
    Write-Host ("  bridge_uptime : {0}s" -f $up.bridge_uptime_s)

    if (-not $want) {
        Write-Host "  -> note: no source checkout at $source; version unchecked" -ForegroundColor Yellow
    } elseif ($up.version -ne $want) {
        Write-Host ("  -> WARNING: serving {0}, source is {1} -- restart the task to pick up the new code" -f $up.version, $want) -ForegroundColor Yellow
    }

    if ($SessionId) {
        if ($up.resuming -eq $SessionId -or $up.session_id -eq $SessionId) {
            Write-Host "  -> session survived the reboot." -ForegroundColor Green
        } elseif ($up.session_state -eq "fresh") {
            Write-Host "  -> WARNING: started fresh; the saved session was not picked up." -ForegroundColor Yellow
        } else {
            Write-Host ("  -> WARNING: expected session {0}" -f $SessionId) -ForegroundColor Yellow
        }
    }
} else {
    Write-Host "BRIDGE IS NOT ANSWERING on $baseUrl" -ForegroundColor Red
    Write-Host "  Check the log below before concluding it is dead: if it says"
    Write-Host "  'cannot bind ... yet' with no 'giving up', it is still waiting"
    Write-Host "  for the tailnet address and will come up on its own."
}

# --- the scheduled task ----------------------------------------------------
Write-Section "task"
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Host "no scheduled task named '$TaskName' -- run install-task.ps1" -ForegroundColor Red
} else {
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    Write-Host ("state       : {0}" -f $task.State)
    Write-Host ("last run    : {0}" -f $info.LastRunTime)
    Write-Host ("last result : 0x{0:X}" -f $info.LastTaskResult)
    if ($info.LastRunTime) {
        Write-Host ("delay from boot: {0:N0}s" -f ($info.LastRunTime - $boot).TotalSeconds)
    }
    # 0x800710E0 is "the operator or administrator has refused the request".
    # With MultipleInstances=IgnoreNew and a repeating heal trigger, that is
    # what a HEALTHY bridge records every time the trigger fires against an
    # instance that is already running. It reads like a failure and is not.
    # Compare as text: LastTaskResult comes back as a signed int, so the
    # obvious `-eq 0x800710E0` silently never matches.
    if (("0x{0:X}" -f $info.LastTaskResult) -eq "0x800710E0" -and $task.State -eq "Running") {
        Write-Host "  -> 0x800710E0 + Running = the heal trigger being refused because" -ForegroundColor DarkGray
        Write-Host "     the bridge is already up. This is the healthy steady state." -ForegroundColor DarkGray
    }
}

# --- what the log says since boot -----------------------------------------
Write-Section "bridge log since boot"
function Get-LinesSinceBoot($path) {
    if (-not (Test-Path $path)) { return $null }
    Get-Content $path | Where-Object {
        if ($_ -match '^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]') {
            $stamp = [datetime]::ParseExact($matches[1], 'yyyy-MM-dd HH:mm:ss', $null)
            $stamp -ge $boot
        } else { $false }
    }
}
$since = Get-LinesSinceBoot $LogFile
if ($null -eq $since)    { Write-Host "(no log file at $LogFile)" }
elseif (-not $since)     { Write-Host "(no lines since boot -- the bridge never started)" -ForegroundColor Red }
else                     { $since | Select-Object -First 40 }

Write-Host ""
Write-Host "how to read the above:" -ForegroundColor DarkGray
Write-Host "  'cannot bind ... yet' then 'serving on' -> the bind race was real, retry won" -ForegroundColor DarkGray
Write-Host "  'starting;' then 'serving on', no retry -> no race; a boot failure is elsewhere" -ForegroundColor DarkGray
Write-Host "  'giving up binding' + FATAL              -> the window was too short, not the cause" -ForegroundColor DarkGray
Write-Host "  FATAL + any other traceback              -> the real cause, named at last" -ForegroundColor DarkGray

if (Test-Path $StderrLog) {
    Write-Section "stderr log (only if the task wraps in cmd.exe)"
    Get-Content $StderrLog -Tail 20
}

# --- orphans ---------------------------------------------------------------
# Counting `claude` processes is not good enough. You almost certainly have an
# interactive Claude Code session open -- that is how these results get read --
# and it is a `claude` process like any other. The earlier version of this
# check called that an orphan every single time.
#
# Parentage is the honest test: an orphan is a child whose bridge is gone, not
# merely a second process with the same name.
Write-Section "orphan check"
$claudeProcs = Get-CimInstance Win32_Process -Filter "Name='claude.exe'" -ErrorAction SilentlyContinue
if (-not $claudeProcs) {
    Write-Host "(no claude process)"
} else {
    $bridgePids = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
                    Where-Object { $_.CommandLine -like "*claude_bridge.py*" } |
                    Select-Object -ExpandProperty ProcessId)

    $rows = foreach ($proc in $claudeProcs) {
        $parent = Get-Process -Id $proc.ParentProcessId -ErrorAction SilentlyContinue
        $verdict =
            if ($bridgePids -contains $proc.ParentProcessId) { "bridge child" }
            elseif (-not $parent)                            { "ORPHAN (parent gone)" }
            else                                             { "not ours ($($parent.ProcessName))" }
        [pscustomobject]@{
            Pid     = $proc.ProcessId
            Parent  = $proc.ParentProcessId
            Started = $proc.CreationDate
            Verdict = $verdict
        }
    }
    $rows | Format-Table -AutoSize

    $orphans = @($rows | Where-Object { $_.Verdict -like "ORPHAN*" })
    $owned   = @($rows | Where-Object { $_.Verdict -eq "bridge child" })
    if ($orphans) {
        Write-Host "$($orphans.Count) orphaned child(ren) -- a previous bridge died and left them." -ForegroundColor Yellow
        Write-Host "A restarted bridge can race these. Stop them if the bridge is behaving oddly." -ForegroundColor Yellow
    }
    if ($owned.Count -gt 1) {
        Write-Host "the bridge has $($owned.Count) children; it should have exactly one." -ForegroundColor Yellow
    }
    if (-not $orphans -and $owned.Count -le 1) {
        Write-Host "no orphans. 'not ours' rows are your own sessions, not a problem." -ForegroundColor DarkGray
    }
}

Write-Host ""
Write-Host "Done." -ForegroundColor Cyan
