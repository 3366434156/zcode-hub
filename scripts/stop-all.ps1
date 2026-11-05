# ============================================================
#  zcode-hub - stop every instance on this machine
#
#  Order matters: supervisors first, gateways second.
#  Killing only the gateway lets its supervisor resurrect it
#  within ~2s, and the old instance then wins the port race
#  against whatever is starting next.
#
#  Gateways are stopped GRACEFULLY first: POST /admin/api/shutdown sets
#  uvicorn's should_exit, so in-flight SSE streams finish and idle
#  keep-alive sockets close with FIN instead of RST. A hard
#  TerminateProcess (Stop-Process -Force) makes clients report
#  "connection lost mid-reply". Only leftovers are force-killed.
#
#  Usage: powershell -NoProfile -ExecutionPolicy Bypass -File scripts\stop-all.ps1 [port] [-Key <adminKey>] [-DrainSeconds <n>]
# ============================================================

param(
    [int]$Port = 3000,
    [string]$Key = "zcode",
    [int]$DrainSeconds = 15
)

$ErrorActionPreference = "Continue"

function Get-Targets {
    Get-CimInstance Win32_Process | Where-Object {
        $_.Name -in @('python.exe', 'pythonw.exe') -and (
            $_.CommandLine -like '*supervisor.py*' -or
            $_.CommandLine -like '*cli.py*serve*'
        )
    }
}

function Get-Gateways {
    @(Get-Targets | Where-Object { $_.CommandLine -like '*cli.py*serve*' })
}

$killed = 0

# 1) supervisors first (a live supervisor would restart its gateway)
$supervisors = @(Get-Targets | Where-Object { $_.CommandLine -like '*supervisor.py*' })
foreach ($p in $supervisors) {
    Write-Host ("  stopping supervisor: PID {0}" -f $p.ProcessId)
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    $killed++
}
if ($supervisors.Count -gt 0) { Start-Sleep -Milliseconds 800 }

# 2) graceful drain: let in-flight streams finish before the process exits
$gateways = Get-Gateways
if ($gateways.Count -gt 0) {
    $requested = $false
    try {
        $headers = @{ Authorization = "Bearer $Key" }
        Invoke-RestMethod -Uri ("http://127.0.0.1:{0}/admin/api/shutdown" -f $Port) `
            -Method Post -Headers $headers -TimeoutSec 3 | Out-Null
        $requested = $true
        Write-Host "  graceful shutdown requested; draining in-flight requests ..."
    } catch {
        Write-Host "  graceful shutdown unavailable; will force stop."
    }
    if ($requested) {
        $deadline = (Get-Date).AddSeconds([Math]::Max(1, $DrainSeconds))
        while ((Get-Date) -lt $deadline) {
            if ((Get-Gateways).Count -eq 0) { break }
            Start-Sleep -Milliseconds 300
        }
    }
}

# 3) force-kill leftovers (loop: a late respawn can appear between rounds)
for ($round = 1; $round -le 5; $round++) {
    $targets = @(Get-Targets)
    if ($targets.Count -eq 0) { break }
    foreach ($p in $targets) {
        Write-Host ("  stopping: PID {0}" -f $p.ProcessId)
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
        $killed++
    }
    Start-Sleep -Milliseconds 800
}

if ($killed -eq 0) {
    Write-Host "  no running zcode-hub instance."
} else {
    Write-Host ("  stopped {0} process(es)." -f $killed)
}

# wait for the listening socket to disappear
for ($i = 0; $i -lt 12; $i++) {
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if (-not $conn) {
        Write-Host ("  port {0} is free." -f $Port)
        exit 0
    }
    Start-Sleep -Milliseconds 500
}

$owner = (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1).OwningProcess
Write-Host ("  [WARN] port {0} is still held by PID {1} - that process is not a zcode-hub instance." -f $Port, $owner)
exit 1
