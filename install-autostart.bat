@echo off
setlocal EnableExtensions
rem ===========================================================
rem  zcode-hub - install logon autostart (windowless)
rem
rem  Registers a Scheduled Task that starts the gateway at user
rem  logon, detached from any console: closing terminals, closing
rem  the old launcher window or rebooting will no longer take the
rem  gateway down. Run once; run with /remove to undo.
rem
rem  ASCII-only on purpose: .bat files are parsed using the
rem  console code page, so non-ASCII text breaks the parser.
rem ===========================================================

if /i "%~1"=="/remove" goto remove

set "HERE=%~dp0"
set "PYEXE=%HERE%.venv\Scripts\python.exe"
if not exist "%PYEXE%" set "PYEXE=python"

set "ACT=subprocess.run(['%PYEXE%','%HERE%scripts\supervisor.py','--port','3000'],cwd=r'%HERE%')"
set "ACT=%ACT:'=''%"
set "ACT=%ACT:''=''%"

echo Installing scheduled task "zcode-hub" (starts gateway at logon, no window) ...
schtasks /Create /F /TN "zcode-hub" /SC ONLOGON /RL LIMITED ^
  /TR "powershell -NoProfile -WindowStyle Hidden -Command \"Start-Process -WindowStyle Hidden -FilePath '%PYEXE%' -ArgumentList 'scripts/supervisor.py','--port','3000' -WorkingDirectory '%HERE%'\""
if errorlevel 1 goto failed

echo.
echo Done. The gateway now starts automatically at logon.
echo   - Remove it : install-autostart.bat /remove
echo   - Logs      : data\logs\gateway-YYYYMMDD.jsonl + supervisor.log
echo   - Console   : the panel is at http://127.0.0.1:3000/admin/login
echo.
echo NOTE: close the old launcher window (or restart) so the two
echo       instances do not fight over port 3000.
pause
exit /b 0

:remove
schtasks /Delete /F /TN "zcode-hub" >nul 2>nul
if errorlevel 1 (
  echo No task named "zcode-hub" was registered.
) else (
  echo Scheduled task "zcode-hub" removed.
)
pause
exit /b 0

:failed
echo.
echo [ERROR] Could not create the scheduled task.
echo         Try running this script once as Administrator.
echo.
pause
exit /b 1
