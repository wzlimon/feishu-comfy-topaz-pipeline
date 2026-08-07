@echo off
chcp 65001 >nul 2>&1
set PYTHONUTF8=1
cd /d "%~dp0"
title Install Dependencies

echo Preparing virtual environment...
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found in PATH. Add Python to PATH or run it manually.
    pause
    exit /b 1
)
set PY=python

if not exist ".venv\Scripts\python.exe" (
    "%PY%" -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual environment. Check Python availability.
        pause
        exit /b 1
    )
)

.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt

echo.
echo Dependencies installed. Next, run the health-check batch file.
pause
