# 网关看门狗常驻循环（用户级，不需要管理员）
#
# 计划任务需要管理员权限（本机 Register-ScheduledTask / schtasks 均 Access denied），
# 因此改为「启动文件夹里的快捷方式」在登录时拉起本循环：每 5 分钟调用一次
# gateway-watchdog.ps1。网关本身由 supervisor.py 托管；本循环只兜底 supervisor
# 也挂掉/被关掉的情况。
#
# 安装：scripts/install-watchdog.ps1（创建启动文件夹快捷方式）
# 卸载：删除 %APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\zcode-hub-watchdog.lnk

$ErrorActionPreference = "Continue"

$mutex = New-Object System.Threading.Mutex($false, "Local\zcode-hub-watchdog-loop")
if (-not $mutex.WaitOne(0)) { exit 0 }

$watchdog = Join-Path $PSScriptRoot "gateway-watchdog.ps1"
while ($true) {
    try {
        & $watchdog
    } catch {
        try {
            $log = Join-Path (Split-Path -Parent $PSScriptRoot) "data\logs\watchdog.log"
            $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
            Add-Content -Encoding UTF8 -Path $log -Value "[$stamp] 看门狗循环异常：$($_.Exception.Message)"
        } catch {}
    }
    Start-Sleep -Seconds 300
}
