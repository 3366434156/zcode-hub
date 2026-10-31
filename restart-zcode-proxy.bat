@echo off
setlocal EnableExtensions
rem ===========================================================
rem  zcode-hub - restart
rem
rem  Stops every zcode-hub instance on this machine (from any
rem  folder - that copy would otherwise keep holding the port),
rem  then launches start-zcode-proxy.bat again.
rem
rem  Usage: restart-zcode-proxy.bat [port]
rem         (optional, forwarded to the launcher)
rem
rem  ASCII-only on purpose: .bat files are parsed using the
rem  console code page, so non-ASCII text breaks the parser.
rem ===========================================================

echo Stopping other zcode-hub instances ...
powershell -NoProfile -Command "$p = Get-CimInstance Win32_Process | Where-Object { $_.Name -in @('python.exe','pythonw.exe') -and $_.CommandLine -like '*cli.py*serve*' }; if ($p) { $p | ForEach-Object { Write-Host ('  stopping: PID ' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; Start-Sleep -Seconds 2; if (Get-CimInstance Win32_Process | Where-Object { $_.Name -in @('python.exe','pythonw.exe') -and $_.CommandLine -like '*cli.py*serve*' }) { Write-Host '  [WARN] some zcode-hub processes survived; they may still hold the port.' } else { Write-Host '  all zcode-hub instances stopped.' } } else { Write-Host '  no running zcode-hub instance.' }"

echo.
echo Starting zcode-hub ...
call "%~dp0start-zcode-proxy.bat" %*
