<#
  zcode-hub 网关看门狗（计划任务每 5 分钟跑一次）

  为什么需要：网关是 5 路续派的额度/上游唯一入口。它一停，Stop 钩子和唤醒器
  都读不到额度，会话就会集体静默停摆。当前网关可能由 launcher 直接拉起（没有
  supervisor），窗口一关/进程一崩就没人重启。

  行为：
  - GET /healthz 200 → 正常，退出；
  - 端口在监听但 /healthz 失败 → 只记日志，不杀进程（避免误杀正在启动的实例）；
  - 端口没人听 → 用 .venv 的 python 隐藏启动 scripts/supervisor.py（自带 50 次重启）；
  - 已有 supervisor 进程 → 不重复启动。

  手动跑：powershell -NoProfile -ExecutionPolicy Bypass -File scripts\gateway-watchdog.ps1
#>
param(
    [int]$Port = 3000,
    [int]$TimeoutSec = 4
)

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
$LogDir = Join-Path $Root "data\logs"
$LogFile = Join-Path $LogDir "watchdog.log"

function Write-Log([string]$Message) {
    try {
        New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
        if ((Test-Path $LogFile) -and ((Get-Item $LogFile).Length -gt 2MB)) {
            Move-Item -Force -Path $LogFile -Destination "$LogFile.1"
        }
        $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
        Add-Content -Encoding UTF8 -Path $LogFile -Value "[$stamp] $Message"
    } catch {}
}

function Test-Health {
    try {
        $resp = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$Port/healthz" `
            -TimeoutSec $TimeoutSec
        return ($resp.StatusCode -eq 200)
    } catch {
        return $false
    }
}

function Test-Port {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $client.Connect("127.0.0.1", $Port)
        return $true
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

if (Test-Health) {
    exit 0
}

if (Test-Port) {
    Write-Log "端口 $Port 在监听，但 /healthz 无响应（进程可能卡死/正在启动）；本轮不重启"
    exit 0
}

$supervisor = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*supervisor.py*" })
if ($supervisor.Count -gt 0) {
    Write-Log "端口 $Port 未监听，但已有 supervisor 进程（PID $($supervisor[0].ProcessId)）；等它拉起"
    exit 0
}

$python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    $python = "python"
}

Write-Log "网关不可用（端口 $Port 未监听）；启动 supervisor：$python"
try {
    Start-Process -WindowStyle Hidden -FilePath $python `
        -ArgumentList @("scripts/supervisor.py", "--port", "$Port") `
        -WorkingDirectory $Root | Out-Null
} catch {
    Write-Log "启动 supervisor 失败：$($_.Exception.Message)"
    exit 1
}

for ($i = 0; $i -lt 15; $i++) {
    Start-Sleep -Seconds 2
    if (Test-Health) {
        Write-Log "网关已恢复（等待 $($i * 2 + 2) 秒）"
        exit 0
    }
}

Write-Log "supervisor 已启动，但 30 秒内 /healthz 仍未就绪；查看 data\logs\supervisor.log"
exit 1
