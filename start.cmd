@echo off
setlocal
cd /d "%~dp0"

echo.
echo  Starting PingHub...
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\start.ps1" %*

echo.
pause
