# 安装/卸载用户级网关看门狗（启动文件夹快捷方式，不需要管理员）
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install-watchdog.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install-watchdog.ps1 -Remove
#
# 安装后：登录时自动拉起 gateway-watchdog-loop.ps1，每 5 分钟探一次 /healthz；
# 网关/supervisor 挂掉会被拉起。立即生效（安装时顺带启动当前会话的循环）。

param([switch]$Remove)

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
$Startup = [Environment]::GetFolderPath("Startup")
$Link = Join-Path $Startup "zcode-hub-watchdog.lnk"

if ($Remove) {
    if (Test-Path $Link) {
        Remove-Item -Force $Link
        Write-Output "已删除启动项：$Link"
    } else {
        Write-Output "启动项不存在：$Link"
    }
    $procs = @(Get-CimInstance Win32_Process |
        Where-Object { $_.CommandLine -like "*gateway-watchdog-loop.ps1*" })
    foreach ($p in $procs) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
    Write-Output "已停止 $($procs.Count) 个看门狗循环进程"
    exit 0
}

$loop = Join-Path $PSScriptRoot "gateway-watchdog-loop.ps1"
if (-not (Test-Path $loop)) {
    Write-Error "缺少 $loop"
    exit 1
}

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($Link)
$shortcut.TargetPath = (Get-Command powershell.exe).Source
$shortcut.Arguments = '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $loop + '"'
$shortcut.WorkingDirectory = $Root
$shortcut.WindowStyle = 7
$shortcut.Description = "zcode-hub 网关看门狗（每 5 分钟探活）"
$shortcut.Save()
Write-Output "已创建启动项：$Link"

$running = @(Get-CimInstance Win32_Process |
    Where-Object { $_.CommandLine -like "*gateway-watchdog-loop.ps1*" })
if ($running.Count -eq 0) {
    Start-Process -WindowStyle Hidden -FilePath $shortcut.TargetPath `
        -ArgumentList $shortcut.Arguments -WorkingDirectory $Root | Out-Null
    Write-Output "已启动看门狗循环（当前会话）"
} else {
    Write-Output "看门狗循环已在运行（PID $($running[0].ProcessId)）"
}
