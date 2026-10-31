@echo off
setlocal EnableExtensions
rem ===========================================================
rem  zcode-hub - start with public access (Cloudflare Tunnel)
rem
rem  Same as start-zcode-proxy.bat, plus one extra thing: right
rem  after the gateway is up, cloudflared opens an outbound
rem  tunnel and Cloudflare hands back a public https address, so
rem  clients on OTHER networks (mobile data, office, a friend's
rem  place) can reach this gateway without a public IP, without
rem  a router port-forward and without touching the firewall.
rem
rem  Where to read the address:
rem    - this console window (line: [OK] tunnel ... https://...)
rem    - admin console: Settings -> "Public access"
rem
rem  Notes:
rem    - quick tunnel address is random and dies with this window.
rem      For a stable domain, create a tunnel in the Cloudflare
rem      dashboard and paste its token in Settings -> Public access.
rem    - before exposing, change the default admin password and
rem      make sure the gateway API Key is set.
rem
rem  Usage: double-click, or  start-public.bat [port]
rem
rem  ASCII-only on purpose: .bat files are read using the
rem  console code page, so non-ASCII text breaks the parser.
rem ===========================================================

set "PORT=%~1"
if "%PORT%"=="" set "PORT=3000"

set "ZCODE_TUNNEL=1"

echo ===========================================================
echo   zcode-hub - public access mode
echo.
echo   A public https address will appear below once
echo   cloudflared connects (usually 3-8 seconds). Look for a
echo   line starting with [OK] tunnel and an https:// URL.
echo.
echo   Also shown in the admin console:
echo     http://127.0.0.1:%PORT%/admin/login  ->  Settings
echo.
echo   Requirements: cloudflared on PATH.
echo     winget install Cloudflare.cloudflared
echo.
echo   Before exposing: change the default admin password and
echo   set the gateway API Key (Settings -> auth).
echo ===========================================================
echo.

call "%~dp0start-zcode-proxy.bat" %*
