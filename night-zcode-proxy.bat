@echo off
setlocal EnableExtensions
rem ===========================================================
rem  zcode-hub - overnight batch runner
rem  Usage: double-click, or  night-zcode-proxy.bat [until] [workers] [ctx-chars]
rem         until     local time to stop        default 08:40
rem         workers   concurrency = #accounts   default 5  (one chat per account)
rem         ctx-chars filler context per request, default 0
rem                   40000 chars ~= 26000 input tokens per request; input
rem                   tokens are metered too, so this burns several times
rem                   faster than output-only mode
rem
rem  Starts the gateway if it is not already listening, then runs
rem  scripts\night-run.py until the deadline. Outputs land in
rem  night-out\ (one .md per answer) so the spent quota leaves
rem  something behind.
rem
rem  ASCII-only on purpose: .bat files are read using the
rem  console code page, so non-ASCII text breaks the parser.
rem  No %VAR% inside if( ... ) blocks: a path containing ( or )
rem  breaks the cmd.exe parser. All checks use goto/errorlevel.
rem ===========================================================

set "UNTIL=%~1"
if "%UNTIL%"=="" set "UNTIL=08:40"
set "WORKERS=%~2"
if "%WORKERS%"=="" set "WORKERS=5"
set "CTX=%~3"
if "%CTX%"=="" set "CTX=0"
set "HERE=%~dp0"
set "PORT=3000"

if not exist "%HERE%scripts\night-run.py" goto no_runner

rem ---- start the gateway when nothing answers on the port ----
powershell -NoProfile -Command "try { Invoke-WebRequest ('http://127.0.0.1:%PORT%/meta') -UseBasicParsing -TimeoutSec 2 | Out-Null; exit 0 } catch { exit 1 }"
if not errorlevel 1 goto gateway_up

echo Gateway not running - starting it in a separate window ...
start "zcode-hub gateway" /d "%HERE%" cmd /c start-zcode-proxy.bat %PORT%
powershell -NoProfile -Command "$ok=$false; 1..20 | ForEach-Object { if(-not $ok){ try { Invoke-WebRequest ('http://127.0.0.1:%PORT%/meta') -UseBasicParsing -TimeoutSec 2 | Out-Null; $ok=$true } catch { Start-Sleep -Seconds 1 } } }; if($ok){ exit 0 } else { exit 1 }"
if errorlevel 1 goto gateway_dead

:gateway_up
echo ===========================================================
echo   overnight quota run
echo   stop at : %UNTIL%      workers: %WORKERS%      ctx chars: %CTX%
echo   target  : http://127.0.0.1:%PORT% / GLM-5.3-Flash
echo   output  : %HERE%night-out
echo.
echo   Press Ctrl+C to stop early.
echo ===========================================================
echo.

set "PYEXE=%HERE%.venv\Scripts\python.exe"
if not exist "%PYEXE%" set "PYEXE=python"
"%PYEXE%" "%HERE%scripts\night-run.py" --until %UNTIL% --workers %WORKERS% --context-chars %CTX% --out "%HERE%night-out"
set "RC=%ERRORLEVEL%"

echo.
echo [batch run finished with code %RC%]
pause
exit /b %RC%

:gateway_dead
echo [ERROR] gateway did not come up on port %PORT%.
echo         Start start-zcode-proxy.bat manually and read its window.
echo.
pause
exit /b 1

:no_runner
echo [ERROR] scripts\night-run.py not found next to this script.
echo.
pause
exit /b 1
