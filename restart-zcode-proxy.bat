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
rem Supervisors are stopped BEFORE gateways: a live supervisor would
rem otherwise restart its gateway within ~2s and win the port race.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\stop-all.ps1" %*

echo.
echo Starting zcode-hub ...
call "%~dp0start-zcode-proxy.bat" %*
