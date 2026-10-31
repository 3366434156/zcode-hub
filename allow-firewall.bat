@echo off
setlocal EnableExtensions
rem ===========================================================
rem  zcode-hub - let other computers on the LAN open the panel
rem
rem  Double-click this file. Windows asks for administrator rights
rem  (that is what the UAC prompt is for), then it adds:
rem    1) an inbound rule for the Python the gateway listens as
rem    2) an inbound TCP rule for the gateway port
rem  Both are limited to the Private and Public profiles.
rem
rem  Usage:  allow-firewall.bat [port]      default 3000
rem          allow-firewall.bat check       show what is there, no change
rem          allow-firewall.bat remove      delete the two rules
rem
rem  ASCII-only on purpose: .bat files are read with the console
rem  code page, non-ASCII text breaks the parser. No %VAR% inside
rem  if( ... ) blocks: a path with ( or ) breaks the cmd parser.
rem ===========================================================

set "ARG=%~1"
if "%ARG%"=="" set "ARG=3000"
set "PORT=%ARG%"
set "MODE=add"
if /i "%ARG%"=="remove" (
  set "PORT=3000"
  set "MODE=remove"
)
if /i "%ARG%"=="check" (
  set "PORT=3000"
  set "MODE=check"
)

set "HERE=%~dp0"
set "PYEXE=%HERE%.venv\Scripts\python.exe"
if not exist "%PYEXE%" set "PYEXE=python"

rem ---- the interpreter the gateway actually listens as ----
rem .venv\Scripts\python.exe is a launcher that may re-exec another
rem interpreter; the rule has to name the process holding the socket.
set "REALPY="
set "PYFILE=%TEMP%\zcode-hub-py.txt"
powershell -NoProfile -Command "$p=(Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1).OwningProcess; if($p){ (Get-Process -Id $p -ErrorAction SilentlyContinue).Path }" > "%PYFILE%" 2>nul
if exist "%PYFILE%" set /p REALPY=<"%PYFILE%"
if exist "%PYFILE%" del "%PYFILE%" >nul 2>&1
if not defined REALPY (
  "%PYEXE%" -c "import sys;print(sys.executable)" > "%PYFILE%" 2>nul
  if exist "%PYFILE%" set /p REALPY=<"%PYFILE%"
  if exist "%PYFILE%" del "%PYFILE%" >nul 2>&1
)
if not defined REALPY set "REALPY=%PYEXE%"

echo.
echo   gateway port     : %PORT%
echo   python launcher  : %PYEXE%
echo   python real      : %REALPY%
echo.

if /i "%MODE%"=="check" goto check

rem ---- elevate once ----
net session >nul 2>&1
if errorlevel 1 (
  echo Asking Windows for administrator rights ...
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList '%ARG%' -Verb RunAs"
  exit /b
)

if /i "%MODE%"=="remove" goto remove

netsh advfirewall firewall delete rule name="zcode-hub gateway (TCP %PORT%)" >nul 2>&1
netsh advfirewall firewall delete rule name="zcode-hub python" >nul 2>&1

netsh advfirewall firewall add rule name="zcode-hub gateway (TCP %PORT%)" dir=in action=allow protocol=TCP localport=%PORT% profile=private,public description="zcode-hub web panel and API"
if errorlevel 1 goto failed

netsh advfirewall firewall add rule name="zcode-hub python" dir=in action=allow program="%REALPY%" enable=yes profile=private,public description="zcode-hub gateway interpreter"
if errorlevel 1 goto failed

echo [OK] firewall rules added.
goto done

:remove
netsh advfirewall firewall delete rule name="zcode-hub gateway (TCP %PORT%)" >nul 2>&1
netsh advfirewall firewall delete rule name="zcode-hub python" >nul 2>&1
echo [OK] firewall rules removed, if they were there.
goto done

:check
echo   --- existing rules ---
netsh advfirewall firewall show rule name="zcode-hub gateway (TCP %PORT%)"
echo.
netsh advfirewall firewall show rule name="zcode-hub python"
echo.
echo   If neither shows up, other computers are blocked: run this file as administrator.
goto done

:failed
echo.
echo [ERROR] adding a rule failed. Run this file as administrator,
echo         or add the rule by hand in Windows Defender Firewall.

:done
echo.
echo   Panel address for other computers on the same network:
echo     http://^<this computer's IPv4^>:%PORT%/admin
echo   (find the IPv4 with ipconfig, or on the Clients page of the panel)
echo.
echo   Test from the other computer:
echo     curl http://^<that IPv4^>:%PORT%/meta
echo   It should answer {"version":"..."}
echo.
pause
exit /b 0

