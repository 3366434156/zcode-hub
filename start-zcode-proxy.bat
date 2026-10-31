@echo off
setlocal EnableExtensions
rem ===========================================================
rem  zcode-hub (zcode2api) - launcher
rem  Usage: double-click, or  start-zcode-proxy.bat [port]
rem
rem  Gateway endpoints (default port 3000):
rem    /v1/messages            Anthropic Messages (Claude Code, Cline)
rem    /v1/chat/completions    OpenAI Chat Completions
rem    /admin/login            web console
rem
rem  ASCII-only on purpose: .bat files are read using the
rem  console code page, so non-ASCII text breaks the parser.
rem
rem  NOTE: this script puts no %VAR% inside an if( ... ) block,
rem  because a path containing ( or ) breaks the cmd.exe parser.
rem  All checks use if/errorlevel/goto instead.
rem
rem  Runs under scripts/supervisor.py: if the server process dies
rem  it is restarted automatically and every exit is recorded to
rem  data/logs/supervisor.log (so "why did it stop" is answerable).
rem  Closing THIS window still stops everything - use
rem  install-autostart.bat for a windowless logon-start that
rem  survives terminal closes and reboots.
rem ===========================================================

set "PORT=%~1"
if "%PORT%"=="" set "PORT=3000"
set "HERE=%~dp0"

if not exist "%HERE%scripts\supervisor.py" goto no_supervisor

rem ---- 1) project venv (uv venv --python 3.11 .venv) ----
if not exist "%HERE%.venv\Scripts\python.exe" goto try_path
set "PYEXE=%HERE%.venv\Scripts\python.exe"
goto have_py

:try_path
rem ---- 2) python on PATH ----
python --version >nul 2>nul
if errorlevel 1 goto no_python
set "PYEXE=python"

:have_py
echo ===========================================================
echo   zcode-hub - ZCode to Anthropic / OpenAI gateway
echo.
echo   Messages : http://127.0.0.1:%PORT%/v1/messages
echo   OpenAI   : http://127.0.0.1:%PORT%/v1/chat/completions
echo   Admin    : http://127.0.0.1:%PORT%/admin/login
echo   Public   : start-public.bat - Cloudflare tunnel
echo.
echo   Python   : %PYEXE%
echo   Mode     : supervised (auto-restart on exit)
if defined ZCODE_CHROMIUM_PATH echo   Browser  : %ZCODE_CHROMIUM_PATH%
echo.
echo   Keep this window open. Closing it stops the server.
echo   Logs: data\logs\ (survive restarts)
echo   Press Ctrl+C to stop.
echo ===========================================================
echo.

"%PYEXE%" "%HERE%scripts\supervisor.py" --port %PORT%
set "RC=%ERRORLEVEL%"

echo.
echo [supervisor exited with code %RC%]
pause
exit /b %RC%

:no_supervisor
echo [ERROR] scripts\supervisor.py not found next to this script.
echo         expected: %HERE%scripts\supervisor.py
echo.
pause
exit /b 1

:no_python
echo [ERROR] No .venv found and no Python on PATH.
echo.
echo   Run these once in this folder:
echo     uv venv --python 3.11 .venv
echo     uv pip install --python .venv\Scripts\python.exe -r requirements.txt
echo     cd captcha_node
echo     npm install
echo.
pause
exit /b 1
