@echo off
title xTool Camera Bridge for LightBurn
cd /d "%~dp0"

echo ===================================================
echo   xTool F2 Ultra / F1 Ultra Camera Bridge
echo ===================================================
echo.

where python >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Python is not installed or not in PATH!
    echo Please install Python 3.8+ from python.org and check "Add Python to PATH".
    pause
    exit /b 1
)

echo Starting Camera Bridge with Fill Lights ON...
echo.
python bridge.py --ip 192.168.178.39 --port 8080 --interval 2.5 --light 100

pause
